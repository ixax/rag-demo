"""config.yml schema, loading, and the answer-format contract it defines.

Schema + type coercion live in these pydantic models, not as inline `if`
checks in server.py. Reading the file itself is _common.config's
load_raw_config (shared with ingest and the reranker service) -- this
module's load_config() just returns that raw, unvalidated config:
search_tools.reasoning.model isn't meaningful in config.yml itself --
server.py fills it in from OLLAMA_REASONING_MODEL on the raw dict, then calls
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
    # Generation backend for answer_question -- always the local
    # OLLAMA_REASONING_MODEL via Ollama's /api/chat. Required; there is no toggle
    # to skip generation.
    model: str


class GenerationProfile(BaseModel):
    """search_tools.generation -- system_prompt + user_prompt_template for
    answer_question's generation step, plus sampler options and a JSON
    schema for constrained output (Ollama's /api/chat "format" field)."""

    system_prompt: str
    user_prompt_template: str
    # Ollama /api/chat "options" (e.g. temperature, min_p).
    options: dict[str, float] | None = None
    # JSON Schema sent as Ollama's "format" field, constraining generation
    # to that shape -- see parse_structured_answer below.
    response_schema: dict | None = None


class SearchToolsConfig(BaseModel):
    """Everything behind search_documents/answer_question -- retrieval,
    reranking, RAG-answer generation. Grouped under config.yml's
    search_tools key."""

    reranker: RerankerConfig
    retrieval: RetrievalConfig
    reasoning: ReasoningConfig
    generation: GenerationProfile
    source_template: str
    ollama_timeout: float = Field(gt=0)
    generate_timeout: float = Field(gt=0)


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
# search_tools.generation's prompt and response_schema -- libs/ollama.py is
# unaware of the format.


def parse_structured_answer(raw: str) -> tuple[str | None, str, list[int]]:
    """Split the generation step's reply into (reasoning, answer,
    source_ids). search_tools.generation.response_schema (config.yml)
    constrains Ollama's output to a JSON object with those exact keys, so
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
