"""Retrieval config schema, the exact-match helper, and the cheap log-only
status classification shared by RagPipeline (pipeline.py) and server.py.

The actual embed -> Qdrant hybrid search -> rerank chain lives in
pipeline.py's RagPipeline step methods now -- this module only keeps the
pieces that don't belong to any single pipeline step: the config schema
(read by libs/config.py), and two small helpers RagPipeline's
exact_match_search/merge_results steps import.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


def _exact_match_result(hit: Any) -> dict:
    payload = hit.payload or {}
    return {
        "title": payload.get("title"),
        "description": payload.get("description"),
        "source_path": payload.get("source_path"),
        "heading": payload.get("heading"),
        "updated": payload.get("lastmod"),
        "text": payload.get("text"),
        # Synthetic top scores -- an exact identifier match bypasses rerank
        # entirely, so there's no real
        # cosine/cross-encoder score to report.
        "retrieval_score": 1.0,
        "rerank_score": 1.0,
    }


# Gate for the exact-match pass: only single-token
# queries that look identifier-shaped (a CLI flag, or contains a separator
# like a config key/dotted method path would) trigger the extra Qdrant
# round-trip -- ordinary prose queries never do.
def _looks_identifier_shaped(query: str) -> bool:
    q = query.strip()
    if not q or " " in q:
        return False
    return q.startswith("--") or any(c in q for c in "_-.")


def _dense_vector(hit: Any) -> list[float] | None:
    vector = getattr(hit, "vector", None)
    if isinstance(vector, dict):
        return vector.get("dense")
    return vector


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = sum(x * x for x in a) ** 0.5
    norm_b = sum(y * y for y in b) ** 0.5
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


class RetrievalConfig(BaseModel):
    top_k_retrieve: int = Field(gt=0)
    top_k_rerank: int = Field(gt=0)
    # null | int > 0. Caps how many of the top_k_retrieve candidates may come
    # from the same source_path before reranking. null keeps the old
    # uncapped behavior.
    max_chunks_per_source: int | None = Field(default=None, gt=0)
    # null | float in (0, 1]. Cosine-similarity threshold, on dense vectors,
    # for dropping a candidate as a near-duplicate of a higher-ranked one
    # already kept -- e.g. the same explanation repeated near-verbatim
    # across two pages. Compared pairwise against every hit already kept,
    # in hybrid_search's fused rank order, so the higher-ranked hit of a
    # near-duplicate pair always survives. null disables this (no
    # dedup, same as before this existed).
    dedup_similarity_threshold: float | None = Field(default=None, gt=0, le=1)


def retrieval_status(
    results: list[dict],
    *,
    reranker_enabled: bool,
    rerank_confidence_cutoff: float | None,
) -> str:
    """Cheap, log-only status classification for a completed pipeline run --
    foundation for failed-query logging and, eventually, a synonym
    table mined from queries that came back low_confidence/empty. Not
    used for any behavioral decision here, only logged."""
    if not results:
        return "empty"
    if reranker_enabled and rerank_confidence_cutoff is not None:
        top_rerank_score = results[0]["rerank_score"]
        if top_rerank_score is not None and top_rerank_score < rerank_confidence_cutoff:
            return "low_confidence"
    return "ok"
