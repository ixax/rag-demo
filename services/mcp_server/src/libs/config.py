"""config.yml schema, loading, and the answer-format contract it defines.

Schema + type coercion live in these pydantic models, not as inline `if`
checks in server.py. Reading the file itself is _common.config's
load_raw_config (shared with ingest and the reranker service) -- this
module's load_config() just returns that raw, unvalidated config:
search_tools.reasoning.model isn't meaningful in config.yml itself --
server.py fills it in from AI_GATEWAY_REASONING_MODEL on the raw dict, then calls
.validate(MCPServerConfig).
"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, Field

from _common.config import RawConfig, load_raw_config

from .reranker import RerankerConfig
from .retrieval import RetrievalConfig


class ReasoningConfig(BaseModel):
    # Generation backend for answer_question -- always
    # AI_GATEWAY_REASONING_MODEL via the AI gateway's /v1/chat/completions.
    # Required; there is no toggle to skip generation.
    model: str


class GenerationProfile(BaseModel):
    """search_tools.generation -- system_prompt + user_prompt_template for
    answer_question's generation step, plus sampler options and a JSON
    schema for constrained output (sent as the AI gateway's
    response_format.json_schema field)."""

    system_prompt: str
    user_prompt_template: str
    # Sampler options (e.g. temperature, min_p), sent as top-level fields on
    # the AI gateway's /v1/chat/completions request.
    options: dict[str, float] | None = None
    # JSON Schema sent as the AI gateway's response_format.json_schema
    # field, constraining generation to that shape -- see
    # parse_structured_answer below.
    response_schema: dict | None = None


class TypoCorrectionConfig(BaseModel):
    """search_tools.typo_correction -- fuzzy-corrects query tokens against
    vocab.json (built by ingest.py) before sparse vectorization. Pure local
    fuzzy match, no network call, so on by default (see
    libs/typo_correct.py)."""

    enabled: bool = True
    threshold: int = Field(default=85, ge=0, le=100)


class SynonymExpansionConfig(BaseModel):
    """search_tools.synonym_expansion -- appends known synonyms (see
    synonyms.yml) of matched terms to the query before sparse vectorization.
    Local lookup, no network call, so on by default (see
    libs/synonyms.py)."""

    enabled: bool = True


class RouterConfig(BaseModel):
    """search_tools.router -- hardcoded reference example questions for
    query_router.QueryRouter's point-vs-global classification. Both empty
    by default -- classify() then always returns "point" (no examples to
    compare against), which is the same behavior as today until the
    summary index actually branches on the result."""

    point_examples: list[str] = Field(default_factory=list)
    global_examples: list[str] = Field(default_factory=list)


class QueryRewriteConfig(BaseModel):
    """search_tools.query_rewrite -- rephrases the query into a more formal,
    documentation-style query before embedding (see libs/query_rewrite.py).
    Adds an extra LLM call + latency to every query, so opt-in (default
    False), unlike typo_correction/synonym_expansion which are local and on
    by default."""

    enabled: bool = False
    model: str = ""
    system_prompt: str = ""


class SummaryConfig(BaseModel):
    """search_tools.summary -- page-level summary index (written by ingest.py
    into <QDRANT_COLLECTION>_summaries) queried in addition to normal chunk
    retrieval when query_router classifies a query as "global" (see
    libs/query_router.py). `source_template`
    mirrors the top-level source_template but for whole-page summaries
    rather than chunk excerpts, so the generation prompt/LLM can tell them
    apart."""

    limit: int = Field(gt=0)
    source_template: str


class SearchToolsConfig(BaseModel):
    """Everything behind search_documents/answer_question -- retrieval,
    reranking, RAG-answer generation. Grouped under config.yml's
    search_tools key."""

    reranker: RerankerConfig
    retrieval: RetrievalConfig
    reasoning: ReasoningConfig
    generation: GenerationProfile
    source_template: str
    embedding_timeout: float = Field(gt=0)
    generate_timeout: float = Field(gt=0)
    typo_correction: TypoCorrectionConfig = TypoCorrectionConfig()
    synonym_expansion: SynonymExpansionConfig = SynonymExpansionConfig()
    query_rewrite: QueryRewriteConfig = QueryRewriteConfig()
    router: RouterConfig = RouterConfig()
    summary: SummaryConfig


class MCPServerConfig(BaseModel):
    search_tools: SearchToolsConfig


def load_config(path: Path) -> RawConfig:
    return load_raw_config(path)


# --------------------------------------------------------------------------
# Answer parsing
# --------------------------------------------------------------------------
#
# Lives here, not in a generation-specific module, because the shape this
# parses is entirely defined by (and only meaningful together with)
# search_tools.generation's prompt and response_schema -- _common/clients/
# reasoning_client.py's ReasoningClient is unaware of the format.


def parse_structured_answer(raw: str) -> tuple[str | None, str, list[int]]:
    """Split the generation step's reply into (reasoning, answer,
    source_ids). search_tools.generation.response_schema (config.yml)
    constrains the model's output to a JSON object with those exact keys, so
    this is a direct json.loads rather than a tolerant regex scan -- the
    schema, not this parser, is what makes the model's output reliably
    parseable. Falls back to (None, raw, []) on malformed JSON
    (schema-constrained generation can still be truncated by
    generate_timeout / a low token budget before the object closes).

    sources comes back as bare integer ids (the <source id="N"> attribute),
    not formatted "[id] title (path)" strings -- golden-eval showed
    gemma3:4b picks correct ids reliably but can't reliably compose that
    string itself. server.py maps these ids back to citation strings using
    the `chunks` list it already has."""
    raw = raw.strip()
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        return None, raw, []
    if not isinstance(obj, dict) or "answer" not in obj:
        return None, raw, []
    reasoning = obj.get("reasoning")
    sources = obj.get("sources") or []
    return (
        str(reasoning).strip() if reasoning else None,
        str(obj["answer"]).strip(),
        [int(s) for s in sources if isinstance(s, int) or (isinstance(s, str) and s.strip().lstrip("-").isdigit())],
    )
