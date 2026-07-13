"""Cross-encoder loading + scoring. No config/env reads here -- model_name/
max_length/query/documents all come in as arguments."""

from __future__ import annotations

from sentence_transformers import CrossEncoder


def load_model(model_name: str, max_length: int) -> CrossEncoder:
    return CrossEncoder(model_name, max_length=max_length)


def score(model: CrossEncoder, query: str, documents: list[str]) -> list[float]:
    pairs = [(query, doc) for doc in documents]
    # mypy: CrossEncoder.predict's declared input type is a large multimodal
    # (text/image/audio/video) union; list is invariant, so our plain
    # list[tuple[str, str]] doesn't satisfy it even though it's a valid
    # (query, document) pair list at runtime.
    return [float(s) for s in model.predict(pairs)]  # type: ignore[arg-type]
