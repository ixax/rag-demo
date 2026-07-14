"""Reranking config schema + the HTTP client for the standalone reranker
service (see /services/reranker). mcp-server doesn't run the cross-encoder
in-process -- this is a thin network client, so mcp-server doesn't need
sentence-transformers/torch as dependencies.
"""

from __future__ import annotations

import httpx
from pydantic import BaseModel


class RerankerConfig(BaseModel):
    enabled: bool
    confidence_cutoff: float | None = None


class RerankerClient:
    """Wraps an httpx.Client pointed at the reranker service. `url` is a
    full base URL (e.g. "http://reranker:50051") -- passed in, not read
    from the environment here."""

    def __init__(self, url: str) -> None:
        # Reranker model inference can take a while, especially on a cold
        # model load, a loaded host, or (see top_k_retrieve in config.yml)
        # a larger candidate batch -- 30s wasn't enough headroom once
        # top_k_retrieve went from 10 to 20 candidates, observed timing out
        # at exactly 30s. 120s matches the other generation-adjacent
        # timeouts in this codebase (ollama_timeout, generate_timeout).
        self._client = httpx.Client(base_url=url, timeout=120.0)

    def rerank(self, query: str, documents: list[str]) -> list[float]:
        resp = self._client.post("/rerank", json={"query": query, "documents": documents})
        resp.raise_for_status()
        return resp.json()["scores"]
