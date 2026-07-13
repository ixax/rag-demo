"""config.yml schema + loading for the reranker service.

Reading the file itself is _common.config's load_raw_config (shared with
ingest and mcp-server) -- this module just supplies RerankerConfig as the
schema. Validation happens in two steps here, not via load_yaml_config's
one-shot helper: "model" isn't in config.yml at all -- server.py fills it
in from MODEL_RERANKER on the raw dict returned by load_config(), then
calls .validate(RerankerConfig).
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field

from _common.config import RawConfig, load_raw_config


class RerankerConfig(BaseModel):
    model: str
    max_length: int = Field(gt=0)


def load_config(path: Path) -> RawConfig:
    return load_raw_config(path)
