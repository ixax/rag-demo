"""Shared result type for the generation backend (ollama.py) -- lets
server.py report token usage the same way regardless of which backend
produced the answer."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class GenerationResult:
    text: str
    # Always 0 for ollama -- Ollama's /api/chat does report
    # prompt_eval_count/eval_count, but this backend doesn't surface them,
    # by design (see ollama.py).
    input_tokens: int
    output_tokens: int
