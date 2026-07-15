"""config.yml schema, loading, and the answer-format contract it defines.

Schema + type coercion + cross-field rules (e.g. "model is required when
type is anthropic") live in these pydantic models, not as inline `if`
checks in server.py. Reading the file itself is _common.config's
load_raw_config (shared with ingest and the reranker service) -- this
module's load_config() just returns that raw, unvalidated config: neither
search_tools.backend.model nor anthropic_chat.model are meaningful in
config.yml itself -- server.py fills the former in from
MODEL_INSTRUCT_INTERNAL or MODEL_INSTRUCT_EXTERNAL (picked by
search_tools.backend.type) and the latter from MODEL_CHAT, on the raw dict,
then calls .validate(MCPServerConfig).
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from _common.config import RawConfig, load_raw_config

from .reranker import RerankerConfig
from .retrieval import RetrievalConfig


class BackendConfig(BaseModel):
    # "anthropic_token": the Claude Messages API via the `anthropic` SDK
    # (libs/anthropic.py), using ANTHROPIC_API_KEY -- billed per token.
    # "anthropic_subscription": the headless `claude` CLI as a subprocess
    # (libs/claude_cli.py), using CLAUDE_CODE_OAUTH_TOKEN -- billed against
    # a Claude subscription instead of a token balance (the Messages API
    # itself has no subscription-billed mode, regardless of credential
    # type -- see that module's docstring for why this needs a different
    # mechanism, not just a different auth parameter). Both need model
    # below; anthropic_subscription ignores max_tokens (the CLI doesn't
    # expose that control).
    type: Literal["", "ollama", "anthropic_token", "anthropic_subscription"]
    model: str | None = None
    max_tokens: int | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def _anthropic_fields_required(self) -> "BackendConfig":
        if self.type == "anthropic_token":
            if not self.model:
                raise ValueError(f"model is required when type is {self.type!r}")
            if self.max_tokens is None:
                raise ValueError(f"max_tokens is required when type is {self.type!r}")
        elif self.type == "anthropic_subscription":
            if not self.model:
                raise ValueError(f"model is required when type is {self.type!r}")
        return self


class AnthropicChatConfig(BaseModel):
    """Config for the standalone `anthropic_chat` tool -- raw text in, raw
    Claude reply out, no retrieval/RAG context. Independent of `backend`
    above: this tool can run against a different Claude model and a
    different credential (token vs subscription) than whatever generates
    `answer_question`'s RAG answers, since the two are separate use cases."""

    enabled: bool = False
    # "token": Anthropic SDK, billed per token, needs ANTHROPIC_API_KEY (see
    # libs/anthropic.py). "subscription": headless `claude` CLI subprocess,
    # billed against a Claude subscription, needs CLAUDE_CODE_OAUTH_TOKEN
    # (see libs/claude_cli.py -- and note max_tokens below doesn't apply to
    # this path, the CLI doesn't expose that control).
    auth: Literal["token", "subscription"] = "subscription"
    model: str | None = None
    max_tokens: int | None = Field(default=None, gt=0)
    system_prompt: str = ""
    # Subprocess timeout (seconds) for auth: "subscription"'s claude CLI
    # call. Ignored for auth: "token" (the SDK has its own default timeout).
    timeout: float = Field(default=120, gt=0)

    @model_validator(mode="after")
    def _fields_required_when_enabled(self) -> "AnthropicChatConfig":
        if self.enabled:
            if not self.model:
                raise ValueError("model is required when anthropic_chat.enabled is true")
            if self.auth == "token" and self.max_tokens is None:
                raise ValueError("max_tokens is required when anthropic_chat.enabled is true and auth is 'token'")
        return self


class GenerationProfile(BaseModel):
    """One entry of search_tools.generation_profiles -- system_prompt +
    user_prompt_template for a given model family, plus (local/ollama only)
    sampler options and a JSON schema for constrained output. See
    config.yml's generation_profiles comment for why agentic (Claude/Codex)
    and local (Ollama) need different prompts/contracts."""

    system_prompt: str
    user_prompt_template: str
    # Ollama /api/chat "options" (e.g. temperature, min_p). None for the
    # agentic profile -- anthropic_lib/claude_cli_lib don't take options.
    options: dict[str, float] | None = None
    # JSON Schema sent as Ollama's "format" field, constraining generation
    # to that shape. None for the agentic profile, which uses the textual
    # Reasoning/Answer/Sources contract instead (see parse_structured_answer
    # below).
    response_schema: dict | None = None


class SearchToolsConfig(BaseModel):
    """Everything behind search_documents/answer_question -- retrieval,
    reranking, RAG-answer generation. Grouped under config.yml's
    search_tools key, separate from AnthropicChatConfig below (an
    unrelated, retrieval-free tool)."""

    reranker: RerankerConfig
    retrieval: RetrievalConfig
    backend: BackendConfig
    generation_profiles: dict[Literal["agentic", "local"], GenerationProfile]
    source_template: str
    ollama_timeout: float = Field(gt=0)
    generate_timeout: float = Field(gt=0)


class MCPServerConfig(BaseModel):
    search_tools: SearchToolsConfig
    anthropic_chat: AnthropicChatConfig


def load_config(path: Path) -> RawConfig:
    return load_raw_config(path)


def profile_for(backend_type: str) -> Literal["agentic", "local"]:
    """Which search_tools.generation_profiles key a given backend.type uses
    -- "local" for ollama (open-weight models needing a short prompt +
    schema-constrained output), "agentic" for both anthropic_* variants
    (Claude/Codex-class models, textual contract). backend_type "" never
    reaches this (answer_question returns before the generate step)."""
    return "local" if backend_type == "ollama" else "agentic"


# --------------------------------------------------------------------------
# Answer parsing
# --------------------------------------------------------------------------
#
# Lives here, not in a generation-specific module, because the shapes these
# parse are entirely defined by (and only meaningful together with)
# generation_profiles.agentic/.local's prompts and, for local, its
# response_schema -- both libs/ollama.py and libs/anthropic.py are unaware
# of either format. server.py picks the parser matching profile_for()
# above.
#
# Each label is searched for independently (not one anchored match() over
# the whole reply) so a model that prepends chatter before the first
# label, or drops one label but not the others, still yields a partial
# parse instead of falling all the way back to reasoning=None. Only a
# missing "Answer:" label -- the one required section -- triggers that
# fallback.

_REASONING_RE = re.compile(r"Reasoning:\s*(.*?)(?=\n\s*Answer:|\Z)", re.DOTALL | re.IGNORECASE)
_ANSWER_RE = re.compile(r"Answer:\s*(.*?)(?=\n\s*Sources:|\Z)", re.DOTALL | re.IGNORECASE)
_SOURCES_RE = re.compile(r"Sources:\s*(.*)\Z", re.DOTALL | re.IGNORECASE)


def parse_structured_answer(raw: str) -> tuple[str | None, str, list[str]]:
    """Split a generation backend's raw reply into (reasoning, answer,
    sources), per the Reasoning/Answer/Sources labels config.yml's
    system_prompt requires. Falls back to (None, raw, []) if the model
    didn't produce an "Answer:" label at all -- small local models don't
    always comply."""
    raw = raw.strip()
    answer_match = _ANSWER_RE.search(raw)
    if not answer_match:
        return None, raw, []
    reasoning_match = _REASONING_RE.search(raw)
    sources_match = _SOURCES_RE.search(raw)
    sources = (
        [line.strip("- ").strip() for line in sources_match.group(1).splitlines() if line.strip()]
        if sources_match
        else []
    )
    return (
        reasoning_match.group(1).strip() if reasoning_match else None,
        answer_match.group(1).strip(),
        sources,
    )


def parse_structured_answer_json(raw: str) -> tuple[str | None, str, list[int]]:
    """Split the "local" (ollama) profile's reply into (reasoning, answer,
    source_ids). Unlike parse_structured_answer above, this profile's
    response_schema (config.yml) constrains Ollama's output to a JSON object
    with those exact keys, so this is a direct json.loads rather than a
    tolerant regex scan -- the schema, not this parser, is what makes local
    models' output reliably parseable. Falls back to (None, raw, []) on
    malformed JSON (schema-constrained generation can still be truncated by
    generate_timeout / a low token budget before the object closes).

    sources comes back as bare integer ids (the <source id="N"> attribute),
    not formatted "[id] title (path)" strings -- golden-eval showed
    gemma3:4b picks correct ids reliably but can't reliably compose that
    string itself. server.py maps these ids back to citation strings using
    the `chunks` list it already has, mirroring what parse_structured_answer
    gets directly from the agentic profile's regex-parsed text."""
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
