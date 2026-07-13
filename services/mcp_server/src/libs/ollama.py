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


def generate(client: httpx.Client, model: str, system_prompt: str, user_content: str, timeout: float) -> str:
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
    return resp.json()["message"]["content"]
