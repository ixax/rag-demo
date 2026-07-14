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


class SearchToolsConfig(BaseModel):
    """Everything behind search_documents/answer_question -- retrieval,
    reranking, RAG-answer generation. Grouped under config.yml's
    search_tools key, separate from AnthropicChatConfig below (an
    unrelated, retrieval-free tool)."""

    reranker: RerankerConfig
    retrieval: RetrievalConfig
    backend: BackendConfig
    system_prompt: str
    source_template: str
    user_prompt_template: str
    ollama_timeout: float = Field(gt=0)
    generate_timeout: float = Field(gt=0)


class MCPServerConfig(BaseModel):
    search_tools: SearchToolsConfig
    anthropic_chat: AnthropicChatConfig


def load_config(path: Path) -> RawConfig:
    return load_raw_config(path)


# --------------------------------------------------------------------------
# Answer parsing
# --------------------------------------------------------------------------
#
# Lives here, not in a generation-specific module, because the shape it
# parses is entirely defined by (and only meaningful together with)
# MCPServerConfig.system_prompt's Reasoning/Answer/Sources label
# instructions -- both libs/ollama.py and libs/anthropic.py are unaware of
# this format.
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
