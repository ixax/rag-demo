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


class RetrievalConfig(BaseModel):
    top_k_retrieve: int = Field(gt=0)
    top_k_rerank: int = Field(gt=0)
    # null | int > 0. Caps how many of the top_k_retrieve candidates may come
    # from the same source_path before reranking. null keeps the old
    # uncapped behavior.
    max_chunks_per_source: int | None = Field(default=None, gt=0)


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
