"""Shared Ollama HTTP client construction + embedding calls, used by both
ingest (embeds content at ingest time) and mcp-server (embeds queries at
query time) so the two sides stay in lockstep. No environment variables are
read here -- base_url/model/timeout all come in as arguments from each
service's own entrypoint, which is the only place that reads env/config.
`timeout` is each caller's own config.yml `ollama_timeout` -- not a shared
default here, since the two services can tune it independently.

Chat generation (mcp-server only, ingest doesn't need it) stays in
mcp_server/src/libs/ollama.py instead of here.
"""

from __future__ import annotations

import httpx


def build_client(base_url: str, timeout: float) -> httpx.Client:
    return httpx.Client(base_url=base_url, timeout=timeout)


def embed_query(client: httpx.Client, model: str, query: str) -> list[float]:
    resp = client.post("/api/embeddings", json={"model": model, "prompt": query})
    resp.raise_for_status()
    return resp.json()["embedding"]
