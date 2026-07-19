"""Embedding client -- used by both ingest (embeds content at ingest time)
and mcp-server (embeds queries at query time), so the two stay in
lockstep on the same model/vector space.
"""

from __future__ import annotations

from .ai_gateway_client import AIGatewayClient


class EmbeddingClient(AIGatewayClient):
    """Speaks the OpenAI-compatible surface (/v1/embeddings) the AI gateway
    exposes in front of whatever backing model it routes to."""

    def __init__(
        self,
        url: str,
        model: str,
        timeout: float,
        api_key: str,
        auth_header: str,
        auth_value_template: str,
    ) -> None:
        super().__init__(url, timeout, api_key, auth_header, auth_value_template)
        self._model = model

    def embed_query(self, query: str) -> list[float]:
        resp = self._post_with_retry("/v1/embeddings", {"model": self._model, "input": query})
        return resp.json()["data"][0]["embedding"]
