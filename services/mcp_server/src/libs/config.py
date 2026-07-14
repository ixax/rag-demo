"""config.yml schema, loading, and the answer-format contract it defines.

Schema + type coercion + cross-field rules (e.g. "model is required when
type is anthropic") live in these pydantic models, not as inline `if`
checks in server.py. Reading the file itself is _common.config's
load_raw_config (shared with ingest and the reranker service) -- this
module's load_config() just returns that raw, unvalidated config: "model"
isn't in config.yml at all -- server.py fills it in from
MODEL_INSTRUCT_INTERNAL or MODEL_INSTRUCT_EXTERNAL (picked by backend.type)
on the raw dict, then calls .validate(MCPServerConfig).
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
    type: Literal["", "ollama", "anthropic"]
    model: str | None = None
    max_tokens: int | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def _anthropic_fields_required(self) -> "BackendConfig":
        if self.type == "anthropic":
            if not self.model:
                raise ValueError("model is required when type is 'anthropic'")
            if self.max_tokens is None:
                raise ValueError("max_tokens is required when type is 'anthropic'")
        return self


class MCPServerConfig(BaseModel):
    reranker: RerankerConfig
    retrieval: RetrievalConfig
    backend: BackendConfig
    system_prompt: str
    source_template: str
    user_prompt_template: str
    ollama_timeout: float = Field(gt=0)
    generate_timeout: float = Field(gt=0)


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
