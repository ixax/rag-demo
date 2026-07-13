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
    ollama_timeout: float = Field(gt=0)


def load_config(path: Path) -> RawConfig:
    return load_raw_config(path)


# --------------------------------------------------------------------------
# Answer parsing
# --------------------------------------------------------------------------
#
# Lives here, not in a generation-specific module, because the shape it
# parses is entirely defined by (and only meaningful together with)
# MCPServerConfig.system_prompt's Reasoning/Answer/Sources instructions --
# both libs/ollama.py and libs/anthropic.py are unaware of this format.

_ANSWER_SECTIONS_RE = re.compile(
    r"Reasoning:\s*(?P<reasoning>.*?)\s*"
    r"Answer:\s*(?P<answer>.*?)\s*"
    r"(?:Sources:\s*(?P<sources>.*))?$",
    re.DOTALL,
)


def parse_structured_answer(raw: str) -> tuple[str | None, str, list[str]]:
    """Split a generation backend's raw reply into (reasoning, answer,
    sources), per the Reasoning/Answer/Sources format config.yml's
    system_prompt requires. Falls back to (None, raw, []) if the model
    didn't follow the format -- small instruct models don't always comply."""
    match = _ANSWER_SECTIONS_RE.match(raw.strip())
    if not match:
        return None, raw.strip(), []
    sources_block = match.group("sources")
    sources = (
        [line.strip("- ").strip() for line in sources_block.splitlines() if line.strip()]
        if sources_block
        else []
    )
    return match.group("reasoning").strip(), match.group("answer").strip(), sources
