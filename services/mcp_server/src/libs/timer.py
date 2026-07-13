"""Wall-clock timing for the RAG pipeline's `trace` response field."""

from __future__ import annotations

import time


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

    def __exit__(self, *exc_info) -> None:
        duration_ms = (time.monotonic() - self._start) * 1000
        self._timer.trace.append({"step": self._step, "duration_ms": round(duration_ms, 1)})
