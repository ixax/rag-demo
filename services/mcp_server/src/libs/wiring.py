"""Builds a fully-wired RagPipeline from env vars + config.yml -- the one
place server.py (the running MCP server) and interactive_test.py (the
standalone CLI walkthrough, see docker-compose.yml's test_search_pipeline
service) both get their pipeline from, so the two can't drift on how
clients/config get constructed.
"""

from __future__ import annotations

import time
from pathlib import Path

import yaml
from environs import Env
from qdrant_client import QdrantClient

from _common.clients.embedding_client import EmbeddingClient
from _common.clients.reasoning_client import ReasoningClient
from _common.clients.reranker_client import RerankerClient
from _common.logging_config import get_logger
from _common.vocab import VocabWatcher

from .config import MCPServerConfig, load_config
from .pipeline import RagPipeline
from .query_router import QueryRouter

logger = get_logger(__name__)


def build_pipeline() -> tuple[RagPipeline, MCPServerConfig, VocabWatcher]:
    env = Env()
    raw_config = load_config(Path("config.yml"))

    QDRANT_URL = env.str("QDRANT_URL")
    QDRANT_COLLECTION = env.str("QDRANT_COLLECTION")
    # Fixed suffix, not independently configurable -- ingest.py writes the
    # summary index under this exact name alongside the main collection.
    QDRANT_SUMMARY_COLLECTION = f"{QDRANT_COLLECTION}_summaries"
    AI_GATEWAY_URL = env.str("AI_GATEWAY_URL")
    AI_GATEWAY_AUTH_KEY = env.str("AI_GATEWAY_AUTH_KEY")
    AI_GATEWAY_AUTH_HEADER = env.str("AI_GATEWAY_AUTH_HEADER")
    AI_GATEWAY_AUTH_VALUE_TEMPLATE = env.str("AI_GATEWAY_AUTH_VALUE_TEMPLATE")
    AI_GATEWAY_EMBEDDINGS_MODEL = env.str("AI_GATEWAY_EMBEDDINGS_MODEL")
    # Reasoning/generation model -- always required, since generation always
    # runs for answer_question and query_rewrite reuses the same model.
    AI_GATEWAY_REASONING_MODEL = env.str("AI_GATEWAY_REASONING_MODEL")

    # search_tools.reasoning.model/query_rewrite.model aren't meaningful in
    # config.yml itself -- they come from AI_GATEWAY_REASONING_MODEL,
    # injected here before validating.
    raw_config.raw["search_tools"]["reasoning"]["model"] = AI_GATEWAY_REASONING_MODEL
    raw_config.raw["search_tools"]["query_rewrite"]["model"] = AI_GATEWAY_REASONING_MODEL
    config: MCPServerConfig = raw_config.validate(MCPServerConfig)

    # AI_GATEWAY_RERANKER_MODEL is the model alias that routes to the
    # gateway's actual reranker deployment -- only read when
    # search_tools.reranker.enabled is true in config.yml.
    AI_GATEWAY_RERANKER_MODEL = env.str("AI_GATEWAY_RERANKER_MODEL") if config.search_tools.reranker.enabled else None

    qdrant_client = QdrantClient(url=QDRANT_URL)
    embedding_client = EmbeddingClient(
        AI_GATEWAY_URL,
        AI_GATEWAY_EMBEDDINGS_MODEL,
        config.search_tools.embedding_timeout,
        AI_GATEWAY_AUTH_KEY,
        AI_GATEWAY_AUTH_HEADER,
        AI_GATEWAY_AUTH_VALUE_TEMPLATE,
    )
    reasoning_client = ReasoningClient(
        AI_GATEWAY_URL,
        config.search_tools.generate_timeout,
        AI_GATEWAY_AUTH_KEY,
        AI_GATEWAY_AUTH_HEADER,
        AI_GATEWAY_AUTH_VALUE_TEMPLATE,
    )
    reranker_client = (
        RerankerClient(
            AI_GATEWAY_URL,
            AI_GATEWAY_RERANKER_MODEL,
            config.search_tools.reranker.timeout,
            AI_GATEWAY_AUTH_KEY,
            AI_GATEWAY_AUTH_HEADER,
            AI_GATEWAY_AUTH_VALUE_TEMPLATE,
        )
        if AI_GATEWAY_RERANKER_MODEL
        else None
    )

    embed_fn = embedding_client.embed_query

    # vocab.json is written by ingest.py into the shared ingest_state volume
    # (mounted read-only here as /state) -- may not exist yet on a fresh
    # volume before ingest's first run, which VocabWatcher treats as an
    # empty vocabulary rather than an error.
    vocab_watcher = VocabWatcher(Path("/state/vocab.json"))

    # synonyms.yml is a small hand-curated file bind-mounted alongside this
    # package -- loaded once at startup, not hot-reloaded like vocab.json.
    synonyms_path = Path("synonyms.yml")
    synonym_table: dict[str, list[str]] = {}
    if synonyms_path.is_file():
        synonym_table = (yaml.safe_load(synonyms_path.read_text()) or {}).get("synonyms", {})

    query_router = QueryRouter(
        config.search_tools.router.point_examples,
        config.search_tools.router.global_examples,
        embed_fn,
    )

    pipeline = RagPipeline(
        qdrant_client=qdrant_client,
        collection=QDRANT_COLLECTION,
        summary_collection=QDRANT_SUMMARY_COLLECTION,
        embed_fn=embed_fn,
        rerank_fn=reranker_client.rerank if reranker_client else None,
        reasoning_client=reasoning_client,
        query_router=query_router,
        typo_vocab_fn=vocab_watcher.get,
        synonym_table=synonym_table,
        config=config.search_tools,
    )

    _warm_up(query_router, reasoning_client, reranker_client, config)

    return pipeline, config, vocab_watcher


def _warm_up(
    query_router: QueryRouter,
    reasoning_client: ReasoningClient,
    reranker_client: RerankerClient | None,
    config: MCPServerConfig,
) -> None:
    """Pays cold-start costs here, once, instead of on whichever request
    happens to arrive first. Two kinds of cost, both paid the first time a
    given model is actually called:
      1. Ollama loading the model's weights into memory (first call to a
         model that isn't already resident is much slower than every call
         after).
      2. QueryRouter's 12 reference-example embed calls specifically (see
         query_router.py) -- cheap individually, but sequential, so they
         add up even once the embedding model itself is warm.
    Covers every model this pipeline can call: QueryRouter.warm() embeds
    the router's point/global examples (also warms the embedding model
    itself, since that's the same embed_fn embed_query uses), one dummy
    reasoning_client.generate() call warms the reasoning model (used by
    both rewrite_query and generate_answer), and -- if enabled -- one dummy
    reranker_client.rerank() call warms the reranker model.
    """
    logger.info("warmup started")
    overall_start = time.monotonic()

    start = time.monotonic()
    query_router.warm()
    logger.info("warmup step=query_router duration_ms=%.1f", (time.monotonic() - start) * 1000)

    start = time.monotonic()
    reasoning_client.generate(
        model=config.search_tools.reasoning.model,
        system_prompt="Reply with OK.",
        user_content="OK",
        options={"temperature": 0.0},
    )
    logger.info("warmup step=reasoning_model duration_ms=%.1f", (time.monotonic() - start) * 1000)

    if reranker_client is not None:
        start = time.monotonic()
        reranker_client.rerank("warmup", ["warmup"])
        logger.info("warmup step=reranker_model duration_ms=%.1f", (time.monotonic() - start) * 1000)

    logger.info("warmup finished total_duration_ms=%.1f", (time.monotonic() - overall_start) * 1000)
