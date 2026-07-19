"""Reranker client -- wraps an httpx.Client pointed at the AI gateway's
Cohere/HuggingFace-style /rerank endpoint. Only used by mcp-server.
"""

from __future__ import annotations

from _common.logging_config import get_logger

from .ai_gateway_client import AIGatewayClient

logger = get_logger(__name__)


class RerankerClient(AIGatewayClient):
    """`model` is the AI model alias (AI_GATEWAY_RERANKER_MODEL) the gateway
    routes to its actual reranker deployment."""

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

    def rerank(self, query: str, documents: list[str]) -> list[float]:
        resp = self._post_with_retry(
            "/rerank", {"model": self._model, "query": query, "documents": documents}
        )
        body = resp.json()
        logger.info("rerank model=%s id=%s", self._model, body.get("id"))
        # "results" isn't in input order and may omit low-scoring entries
        # (top_n) -- rebuild a full, input-order score list from each
        # result's "index" so callers can zip it back against `documents`.
        scores = [0.0] * len(documents)
        for result in body["results"]:
            scores[result["index"]] = result["relevance_score"]
        return scores
