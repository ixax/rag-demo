"""Retrieval: config schema + the embed -> Qdrant search -> optional rerank
step shared by both MCP tools.
"""

from __future__ import annotations

from typing import Callable

from pydantic import BaseModel, Field
from qdrant_client import QdrantClient

from .timer import StepTimer


class RetrievalConfig(BaseModel):
    top_k_retrieve: int = Field(gt=0)
    top_k_rerank: int = Field(gt=0)


def retrieve(
    query: str,
    top_k_retrieve: int,
    top_k_rerank: int,
    timer: StepTimer,
    *,
    qdrant: QdrantClient,
    collection: str,
    embed_fn: Callable[[str], list[float]],
    reranker_enabled: bool,
    rerank_fn: Callable[[str, list[str]], list[float]] | None = None,
) -> list[dict]:
    """embed -> Qdrant search -> optional rerank. `embed_fn` and `rerank_fn`
    are injected so this stays agnostic to which embedding provider/reranker
    is configured -- server.py wires those up (rerank_fn calls out to the
    standalone reranker HTTP service, see libs/reranker.py)."""
    with timer("embed_query"):
        vector = embed_fn(query)

    with timer("qdrant_search"):
        hits = qdrant.search(collection_name=collection, query_vector=vector, limit=top_k_retrieve)
    if not hits:
        return []

    # reranker_enabled: false skips this step entirely -- results keep plain
    # Qdrant cosine-similarity order and rerank_score is null, instead of
    # calling out to the reranker service.
    if reranker_enabled:
        if rerank_fn is None:
            raise ValueError("rerank_fn is required when reranker_enabled is True")
        texts = [hit.payload.get("text", "") for hit in hits]
        with timer("rerank"):
            scores = rerank_fn(query, texts)
        reranked = sorted(zip(hits, scores), key=lambda pair: pair[1], reverse=True)
        reranked = reranked[:top_k_rerank]
    else:
        reranked = [(hit, None) for hit in hits[:top_k_rerank]]

    results = []
    for hit, rerank_score in reranked:
        payload = hit.payload or {}
        results.append(
            {
                "title": payload.get("title"),
                "source_path": payload.get("source_path"),
                "heading": payload.get("heading"),
                "text": payload.get("text"),
                "retrieval_score": float(hit.score),
                "rerank_score": float(rerank_score) if rerank_score is not None else None,
            }
        )
    return results
