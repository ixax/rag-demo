"""Mutable state threaded through every RagPipeline step (see pipeline.py).

One dataclass instance flows through the whole chain: each step method reads
what it needs off it and writes its own output back onto the same object,
returning it for the next step. When a step's config gates it off (reranker
disabled, no typo vocab, "point"-routed query, want_answer=False, ...) it
just returns the state untouched -- the field it would have set already
carries a default value shaped the way the next step expects it, so no
downstream method needs to special-case "this step didn't run".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from qdrant_client.models import Filter, SparseVector

from .timer import StepTimer


@dataclass
class PipelineState:
    # Request-scoped inputs, set once before run() starts.
    query: str
    top_k_retrieve: int
    top_k_rerank: int
    title: str | None
    description: str | None
    source_path: str | None
    tags: list[str] | None
    want_answer: bool
    timer: StepTimer
    on_step: Callable[[str, float, dict], None] | None = None

    # route_result-equivalent, readable by callers (e.g. server.py) after
    # run() returns, same as the old route_result dict passed into retrieve().
    search_query: str = ""
    vector: list[float] = field(default_factory=list)
    route_type: str = "point"
    lexical_query: str = ""
    sparse_vec: SparseVector | None = None
    query_filter: Filter | None = None
    exact_hits: list[Any] = field(default_factory=list)
    hits: list[Any] = field(default_factory=list)
    reranked: list[tuple[Any, float | None]] = field(default_factory=list)
    results: list[dict] = field(default_factory=list)
    summary_hits: list[dict] = field(default_factory=list)
    context_items: list[dict] = field(default_factory=list)
    context_text: str = ""
    generation_text: str = ""
    input_tokens: int | None = None
    output_tokens: int | None = None
    reasoning: str | None = None
    answer: str = ""
    sources: list[str] = field(default_factory=list)
