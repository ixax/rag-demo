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


def generate(client: httpx.Client, model: str, system_prompt: str, user_content: str, timeout: float) -> GenerationResult:
    resp = client.post(
        "/api/chat",
        json={
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
            "stream": False,
        },
        timeout=timeout,
    )
    resp.raise_for_status()
    # input_tokens/output_tokens zeroed rather than read from
    # prompt_eval_count/eval_count -- token accounting for this backend
    # isn't tracked, by design (it's the free/local path).
    return GenerationResult(text=resp.json()["message"]["content"], input_tokens=0, output_tokens=0)
