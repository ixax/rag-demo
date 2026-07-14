"""Retrieval: config schema + the embed -> Qdrant search -> optional rerank
step shared by both MCP tools.
"""

from __future__ import annotations

from typing import Any, Callable

from pydantic import BaseModel, Field
from qdrant_client import QdrantClient
from qdrant_client.models import FieldCondition, Filter, MatchValue

from _common.logging_config import get_logger

from .timer import StepTimer

logger = get_logger(__name__)


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
    rerank_confidence_cutoff: float | None = None,
    title: str | None = None,
    description: str | None = None,
    source_path: str | None = None,
) -> list[dict]:
    """embed -> Qdrant search -> optional rerank. `embed_fn` and `rerank_fn`
    are injected so this stays agnostic to which embedding provider/reranker
    is configured -- server.py wires those up (rerank_fn calls out to the
    standalone reranker HTTP service, see libs/reranker.py).

    `title`/`description`/`source_path`, when given, exact-match filter the
    vector search itself (not a post-filter) via the keyword payload indexes
    ingest.py creates -- narrows to a known page instead of relying on
    semantic similarity alone."""
    with timer("embed_query"):
        vector = embed_fn(query)
    # Full vectors are 300-1024+ floats -- not human-readable and not worth
    # logging in full. A dimension + short fingerprint (first 4 components)
    # is enough to confirm embedding actually ran and roughly what it
    # produced, without dumping an unreadable wall of numbers per request.
    logger.info(
        "embed_query query=%r dim=%d fingerprint=%s",
        query,
        len(vector),
        [round(v, 4) for v in vector[:4]],
    )

    conditions: list[FieldCondition] = []
    if title is not None:
        conditions.append(FieldCondition(key="title", match=MatchValue(value=title)))
    if description is not None:
        conditions.append(FieldCondition(key="description", match=MatchValue(value=description)))
    if source_path is not None:
        conditions.append(FieldCondition(key="source_path", match=MatchValue(value=source_path)))
    # mypy: Filter.must's declared type is a broader Condition union: list is
    # invariant, so list[FieldCondition] doesn't satisfy it even though every
    # element is a valid Condition at runtime.
    query_filter = Filter(must=conditions) if conditions else None  # type: ignore[arg-type]

    with timer("qdrant_search"):
        hits = qdrant.search(
            collection_name=collection, query_vector=vector, query_filter=query_filter, limit=top_k_retrieve
        )
    if not hits:
        return []

    # reranker_enabled: false skips this step entirely -- results keep plain
    # Qdrant cosine-similarity order and rerank_score is null, instead of
    # calling out to the reranker service.
    reranked: list[tuple[Any, float | None]]
    if reranker_enabled:
        if rerank_fn is None:
            raise ValueError("rerank_fn is required when reranker_enabled is True")
        texts = [(hit.payload or {}).get("text", "") for hit in hits]
        try:
            with timer("rerank"):
                scores = rerank_fn(query, texts)
        except Exception as exc:
            # Reranker down/unreachable/timed out -- fall back to plain
            # Qdrant order (same shape as reranker_enabled: false) instead of
            # failing the whole request over a non-essential ranking step.
            logger.warning("reranker call failed, falling back to vector-similarity order: %s", exc)
            reranked = [(hit, None) for hit in hits[:top_k_rerank]]
        else:
            ranked = sorted(zip(hits, scores), key=lambda pair: pair[1], reverse=True)
            # Confidence-gated cutoff: if the top hit clears
            # rerank_confidence_cutoff, drop everything below it -- a
            # confidently-relevant match exists, so weaker ones are noise
            # (this is what let e.g. an unrelated "Commit" chunk sit at
            # rerank_score 0.85 next to the actually-relevant "Launch" chunk
            # at 0.98 and still get fed to the generator as if comparably
            # relevant). Skipped when the top score itself doesn't clear the
            # cutoff -- a mediocre top match shouldn't be reduced to nothing.
            if ranked and rerank_confidence_cutoff is not None and ranked[0][1] >= rerank_confidence_cutoff:
                ranked = [pair for pair in ranked if pair[1] >= rerank_confidence_cutoff]
            reranked = list(ranked[:top_k_rerank])
    else:
        reranked = [(hit, None) for hit in hits[:top_k_rerank]]

    results = []
    for hit, rerank_score in reranked:
        payload = hit.payload or {}
        results.append(
            {
                "title": payload.get("title"),
                "description": payload.get("description"),
                "source_path": payload.get("source_path"),
                "heading": payload.get("heading"),
                "updated": payload.get("lastmod"),
                "text": payload.get("text"),
                "retrieval_score": float(hit.score),
                "rerank_score": float(rerank_score) if rerank_score is not None else None,
            }
        )
    return results
