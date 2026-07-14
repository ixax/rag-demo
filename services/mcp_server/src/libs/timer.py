"""Wall-clock timing for the RAG pipeline's `trace` response field -- and,
when OTEL_EXPORTER_OTLP_ENDPOINT is configured, the matching OTel span tree.
Both are two views of the same step boundaries: every `with timer(step):`
block already marks a pipeline stage, so it doubles as a span without
touching any of retrieval.py's/server.py's call sites."""

from __future__ import annotations

import time

from _common.tracing import get_tracer

_tracer = get_tracer(__name__)


class StepTimer:
    """Collects {"step", "duration_ms"} entries for a tool response's
    `trace` -- the call chain with timings the RAG pipeline exposes."""

    def __init__(self) -> None:
        self.trace: list[dict] = []

    def __call__(self, step: str) -> "_Step":
        return _Step(self, step)


class _Step:
    def __init__(self, timer: StepTimer, step: str) -> None:
        self._timer = timer
        self._step = step

    def __enter__(self) -> None:
        self._start = time.monotonic()
        # get_tracer() returns a real no-op tracer when tracing isn't
        # configured, so this context manager is cheap either way.
        self._span_cm = _tracer.start_as_current_span(self._step)
        self._span_cm.__enter__()

    def __exit__(self, *exc_info) -> None:
        duration_ms = (time.monotonic() - self._start) * 1000
        self._timer.trace.append({"step": self._step, "duration_ms": round(duration_ms, 1)})
        self._span_cm.__exit__(*exc_info)
