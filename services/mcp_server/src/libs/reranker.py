"""Reranking config schema for an external reranker HTTP service this repo
doesn't own. The HTTP client itself (RerankerClient) lives in
_common.clients.reranker_client -- mcp-server doesn't run the cross-encoder
in-process, so mcp-server doesn't need sentence-transformers/torch as
dependencies.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class RerankerConfig(BaseModel):
    enabled: bool
    confidence_cutoff: float | None = None
    timeout: float = Field(gt=0)
