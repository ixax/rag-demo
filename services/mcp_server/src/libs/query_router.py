"""Classifies a query as "point" (specific value/parameter) or "global"
(broad/overview) via cosine similarity to a small set of hardcoded reference
example questions. The reference vectors are computed once, either by an
explicit warm() call (see libs/wiring.py's _warm_up(), run at startup so the
cost lands there instead of on the first real request) or lazily on the
first classify() if warm() was never called. Cheap on every call after
that: reuses the query vector retrieve() already computed, no extra embed
call or Qdrant round-trip.
"""

from __future__ import annotations

import math
from typing import Callable, Literal

QueryType = Literal["point", "global"]


def _normalize(vec: list[float]) -> list[float]:
    norm = math.sqrt(sum(v * v for v in vec))
    return [v / norm for v in vec] if norm else vec


def _cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b))


class QueryRouter:
    """Stores point_examples/global_examples at construction time; warm() (or
    the first classify() call) embeds them and caches the vectors, then
    every classify() call does a plain dot-product max-similarity check
    against both reference sets -- vectors are pre-normalized so the dot
    product is already cosine similarity."""

    def __init__(
        self,
        point_examples: list[str],
        global_examples: list[str],
        embed_fn: Callable[[str], list[float]],
    ) -> None:
        self._point_examples = point_examples
        self._global_examples = global_examples
        self._embed_fn = embed_fn
        self._point_vectors: list[list[float]] | None = None
        self._global_vectors: list[list[float]] | None = None

    def warm(self) -> None:
        """Embeds the reference examples now, if not already done. Called
        explicitly at startup by wiring.py; classify() also calls this so
        the router still works correctly (just with first-call latency) if
        warm() was skipped."""
        if self._point_vectors is None or self._global_vectors is None:
            self._point_vectors = [_normalize(self._embed_fn(q)) for q in self._point_examples]
            self._global_vectors = [_normalize(self._embed_fn(q)) for q in self._global_examples]

    def classify(self, query_vector: list[float]) -> QueryType:
        self.warm()
        point_vectors = self._point_vectors or []
        global_vectors = self._global_vectors or []
        if not point_vectors and not global_vectors:
            return "point"
        vec = _normalize(query_vector)
        point_score = max((_cosine(vec, v) for v in point_vectors), default=-1.0)
        global_score = max((_cosine(vec, v) for v in global_vectors), default=-1.0)
        return "global" if global_score > point_score else "point"
