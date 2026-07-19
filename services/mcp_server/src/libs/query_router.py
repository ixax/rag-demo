"""Classifies a query as "point" (specific value/parameter) or "global"
(broad/overview) via cosine similarity to a small set of hardcoded reference
example questions, embedded once at server startup. Cheap at query time:
reuses the query vector retrieve() already computed, no extra embed call
or Qdrant round-trip.
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
    """Embeds point_examples/global_examples once at construction time, then
    classify() does a plain dot-product max-similarity check against both
    reference sets -- vectors are pre-normalized so the dot product is
    already cosine similarity."""

    def __init__(
        self,
        point_examples: list[str],
        global_examples: list[str],
        embed_fn: Callable[[str], list[float]],
    ) -> None:
        self._point_vectors = [_normalize(embed_fn(q)) for q in point_examples]
        self._global_vectors = [_normalize(embed_fn(q)) for q in global_examples]

    def classify(self, query_vector: list[float]) -> QueryType:
        if not self._point_vectors and not self._global_vectors:
            return "point"
        vec = _normalize(query_vector)
        point_score = max((_cosine(vec, v) for v in self._point_vectors), default=-1.0)
        global_score = max((_cosine(vec, v) for v in self._global_vectors), default=-1.0)
        return "global" if global_score > point_score else "point"
