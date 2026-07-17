"""Cross-encoder loading + scoring. No config/env reads here -- model_name/
max_length/query/documents all come in as arguments."""

from __future__ import annotations

import torch
from sentence_transformers import CrossEncoder


def load_model(model_name: str, max_length: int, num_threads: int) -> CrossEncoder:
    # torch.get_num_threads() defaults to the *host's* CPU count (os.cpu_
    # count()), not this container's cgroup cpus limit -- left unset, torch
    # oversubscribes way past the quota docker-compose.yml actually grants
    # this container, and the resulting thread contention/context-switching
    # made raising that quota (1.5 -> 3.0 cpus) yield far less rerank
    # speedup than expected. Both calls must happen before any torch op
    # runs (interop threading is fixed at first use), so this has to be the
    # very first thing that touches torch in the process.
    torch.set_num_threads(num_threads)
    torch.set_num_interop_threads(num_threads)
    return CrossEncoder(model_name, max_length=max_length)


def score(model: CrossEncoder, query: str, documents: list[str]) -> list[float]:
    pairs = [(query, doc) for doc in documents]
    # mypy: CrossEncoder.predict's declared input type is a large multimodal
    # (text/image/audio/video) union; list is invariant, so our plain
    # list[tuple[str, str]] doesn't satisfy it even though it's a valid
    # (query, document) pair list at runtime.
    return [float(s) for s in model.predict(pairs)]  # type: ignore[arg-type]
