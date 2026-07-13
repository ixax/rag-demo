"""Cross-encoder loading + scoring. No config/env reads here -- model_name/
max_length/query/documents all come in as arguments."""

from __future__ import annotations

from sentence_transformers import CrossEncoder


def load_model(model_name: str, max_length: int) -> CrossEncoder:
    return CrossEncoder(model_name, max_length=max_length)


def score(model: CrossEncoder, query: str, documents: list[str]) -> list[float]:
    pairs = [(query, doc) for doc in documents]
    return [float(s) for s in model.predict(pairs)]
