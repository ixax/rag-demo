"""Ollama-specific calls: chat generation.

Client construction and embedding are shared with ingest (see
_common.ollama, both services embed with the same model) -- this module only
adds generation, which ingest doesn't need.

No environment variables are read here -- model/system_prompt/user_content
all come in as arguments from server.py, which is the only place that reads
env/config.
"""

from __future__ import annotations

import httpx

from .generation import GenerationResult


def generate(
    client: httpx.Client,
    model: str,
    system_prompt: str,
    user_content: str,
    timeout: float,
    options: dict[str, float] | None = None,
    response_schema: dict | None = None,
) -> GenerationResult:
    body: dict = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
        "stream": False,
    }
    # Both optional/omitted by default so a caller that doesn't pass them
    # (or a config.yml search_tools.generation without options/
    # response_schema set) gets Ollama's own defaults rather than this
    # function injecting empty values. options -> sampler params (e.g.
    # temperature, min_p). response_schema -> Ollama's structured-outputs
    # "format" field, which constrains sampling to that JSON shape at the
    # grammar level instead of a textual "reply in this format" instruction.
    if options is not None:
        body["options"] = options
    if response_schema is not None:
        body["format"] = response_schema
    resp = client.post("/api/chat", json=body, timeout=timeout)
    resp.raise_for_status()
    # input_tokens/output_tokens zeroed rather than read from
    # prompt_eval_count/eval_count -- token accounting for this backend
    # isn't tracked, by design (it's the free/local path).
    return GenerationResult(text=resp.json()["message"]["content"], input_tokens=0, output_tokens=0)
