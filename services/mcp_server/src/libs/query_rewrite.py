"""Query rewriting (rewrite-only -- HyDE dropped): rephrases the user's
conversational query into a more formal, documentation-style phrasing
before embedding, using the same reasoning
backend as answer_question's generation step. A cheap quality-of-search
nicety, not allowed to break retrieval -- any failure (timeout, malformed
reply) falls back to the original query untouched.
"""

from __future__ import annotations

from _common.clients.reasoning_client import ReasoningClient
from _common.logging_config import get_logger

logger = get_logger(__name__)


def rewrite_query(query: str, reasoning_client: ReasoningClient, model: str, system_prompt: str) -> str:
    try:
        result = reasoning_client.generate(model, system_prompt, query)
        rewritten = result.text.strip()
    except Exception as exc:
        logger.warning("query_rewrite failed, using original query: %s", exc)
        return query
    return rewritten or query
