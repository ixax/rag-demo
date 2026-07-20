"""MCP server exposing retrieval-augmented search/answering over the Qdrant
collection populated by ../ingest (services/ingest).

Pipeline for both tools:
  1. embed the query with AI_GATEWAY_EMBEDDINGS_MODEL (same model used at ingest time,
     so query and document vectors live in the same space)
  2. fetch TOP_K_RETRIEVE nearest chunks from Qdrant
  3. rerank those candidates via an external reranker HTTP service this repo
     doesn't own, and keep the top TOP_K_RERANK -- skippable via
     config.yml's search_tools.reranker.enabled
  4. `answer_question` additionally feeds the reranked chunks as context to
     the AI_GATEWAY_REASONING_MODEL via the AI gateway's /v1/chat/completions
     and returns its generated answer. This step always runs for
     answer_question.

Both search_documents/answer_question return structured JSON rather than
plain text: the retrieved chunks (the local data the reasoning is grounded
in), the reasoning itself (`answer_question` only -- see config.yml's
search_tools.generation for the JSON-schema-constrained Reasoning/Answer/
Sources contract the model's output follows), and a `trace` of pipeline
steps with wall-clock timings. This is meant to be consumed by a thin
presentation layer (e.g. a Haiku-model agent) rather than reasoned about
further -- the reasoning already happened here.

Exposed over streamable-http so it can be added as a remote MCP connector in
both Open WebUI and Claude (Console / Desktop / claude.ai).

This file is handlers + wiring only. Everything else -- config schema,
retrieval, timing, answer parsing, and the AI gateway client classes --
lives in libs/ and _common/clients/. Env vars and config.yml are read
*only* here; every libs/ function takes what it needs as arguments instead
of reading globals itself.
"""

from __future__ import annotations

import functools
from pathlib import Path
from typing import Callable

import anyio
import anyio.from_thread
import anyio.to_thread
import yaml
from environs import Env
from mcp.server.fastmcp import Context, FastMCP
from opentelemetry.trace import format_trace_id
from qdrant_client import QdrantClient

from _common.clients.embedding_client import EmbeddingClient
from _common.clients.reasoning_client import ReasoningClient
from _common.clients.reranker_client import RerankerClient
from _common.logging_config import configure_logging, get_logger
from _common.tracing import OTEL_EXPORTER_OTLP_ENDPOINT, configure_tracing, get_meter, get_tracer
from _common.vocab import VocabWatcher

from .libs.config import MCPServerConfig, load_config, parse_structured_answer
from .libs.query_rewrite import rewrite_query
from .libs.query_router import QueryRouter
from .libs.retrieval import retrieval_status, retrieve, retrieve_summary
from .libs.timer import StepTimer

# env.str(name)/env.int(name) raise a clear environs.EnvError if the
# variable is unset -- no hand-rolled required-env-var helper (that used to
# be copy-pasted between this file and services/ingest/ingest.py).
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
MCP_HOST = env.str("MCP_HOST")
MCP_PORT = env.int("MCP_PORT")
# Reasoning/generation model for answer_question -- always required, since
# generation always runs.
AI_GATEWAY_REASONING_MODEL = env.str("AI_GATEWAY_REASONING_MODEL")

configure_logging()
logger = get_logger(__name__)

configure_tracing("mcp-server")
tracer = get_tracer(__name__)
_requests_counter = get_meter(__name__).create_counter(
    "rag_requests_total", description="RAG tool calls, by tool and outcome."
)

# RAG-pipeline tuning knobs (reranker on/off, retrieval top-k, system
# prompt) live in config.yml, not the environment -- see that file for the
# full field reference. It's bind-mounted alongside this package, so
# editing it just needs a restart, not a rebuild.
# search_tools.reasoning.model isn't meaningful in config.yml itself -- it
# comes from AI_GATEWAY_REASONING_MODEL, injected here before validating.
# load_config()/.validate() report/exit on a missing file or a schema
# violation themselves, so there's nothing to catch here.
raw_config.raw["search_tools"]["reasoning"]["model"] = AI_GATEWAY_REASONING_MODEL
raw_config.raw["search_tools"]["query_rewrite"]["model"] = AI_GATEWAY_REASONING_MODEL
config: MCPServerConfig = raw_config.validate(MCPServerConfig)

# AI_GATEWAY_RERANKER_MODEL is the model alias that routes to the gateway's
# actual reranker deployment -- only read when search_tools.reranker.enabled
# is true in config.yml.
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

# Bound once so handlers don't need to know which embedding provider is
# configured -- currently always the AI gateway, but this is the seam if
# that ever changes.
_embed_fn = embedding_client.embed_query

# vocab.json is written by ingest.py into the shared ingest_state volume
# (mounted read-only here as /state, see docker-compose.yml) -- may not
# exist yet on a fresh volume before ingest's first run, which VocabWatcher
# treats as an empty vocabulary rather than an error.
_vocab_watcher = VocabWatcher(Path("/state/vocab.json"))

# synonyms.yml is a small hand-curated file bind-mounted alongside this
# package (see docker-compose.yml) -- loaded once at startup, not
# hot-reloaded like vocab.json, since it changes far less often than the
# ingest-derived vocabulary.
_synonyms_path = Path("synonyms.yml")
_synonym_table: dict[str, list[str]] = {}
if _synonyms_path.is_file():
    _synonym_table = (yaml.safe_load(_synonyms_path.read_text()) or {}).get("synonyms", {})

# Reference examples embedded once at startup -- see libs/query_router.py.
_query_router = QueryRouter(
    config.search_tools.router.point_examples,
    config.search_tools.router.global_examples,
    _embed_fn,
)

mcp = FastMCP("rag", host=MCP_HOST, port=MCP_PORT)


def _make_progress_reporter(ctx: Context | None) -> Callable[[str, float, dict], None]:
    """Builds an `on_step` callback for retrieve()/the generate step: reports
    an MCP progress notification the instant each pipeline stage finishes,
    instead of only after the whole tool call returns. Must be called from a
    worker thread (retrieve() and reasoning_client.generate() both block on
    network I/O, so callers run them via anyio.to_thread.run_sync) --
    anyio.from_thread.run() hops back to the event loop thread to actually
    send the notification while that worker thread keeps running the next
    stage. A running step count is kept internally so `progress` climbs
    monotonically across both retrieve()'s internal steps and the separate
    `generate` step in answer_question. `ctx` is None only in the
    unusual case FastMCP has no request context to inject -- on_step is then
    a no-op instead of erroring."""
    steps_done = 0

    def on_step(step: str, duration_ms: float, characteristics: dict) -> None:
        nonlocal steps_done
        if ctx is None:
            return
        steps_done += 1
        details = ", ".join(f"{k}={v}" for k, v in characteristics.items())
        message = f"{step}: {duration_ms:.0f}ms ({details})" if details else f"{step}: {duration_ms:.0f}ms"
        anyio.from_thread.run(ctx.report_progress, steps_done, None, message)

    return on_step


async def _maybe_rewrite_query(query: str, timer: StepTimer, on_step: Callable[[str, float, dict], None]) -> str:
    """search_tools.query_rewrite.enabled gates this -- off by default (see
    config.yml), since it adds an LLM call + latency to every query. When
    on, the rewritten text is what gets embedded/searched, but callers keep
    using the original `query` for logging and (in answer_question) the
    generation step's user_content -- see libs/query_rewrite.py."""
    if not config.search_tools.query_rewrite.enabled:
        return query
    with timer("query_rewrite"):
        rewritten = await anyio.to_thread.run_sync(
            functools.partial(
                rewrite_query,
                query,
                reasoning_client,
                config.search_tools.query_rewrite.model,
                config.search_tools.query_rewrite.system_prompt,
            )
        )
    if rewritten != query:
        logger.info("query_rewrite query=%r rewritten=%r", query, rewritten)
    on_step("query_rewrite", timer.trace[-1]["duration_ms"], {"changed": rewritten != query})
    return rewritten


async def _maybe_summary_search(
    route_result: dict, timer: StepTimer, on_step: Callable[[str, float, dict], None]
) -> list[dict]:
    """Only queries the summary index when retrieve()'s router classified
    the query as "global" -- reuses the query vector retrieve() already
    stashed into `route_result` (see libs/retrieval.py), no extra embed
    call. Returns [] for "point" queries, same shape as today."""
    if route_result.get("type") != "global":
        return []
    return await anyio.to_thread.run_sync(
        functools.partial(
            retrieve_summary,
            route_result["vector"],
            config.search_tools.summary.limit,
            timer,
            qdrant_client=qdrant_client,
            collection=QDRANT_SUMMARY_COLLECTION,
            on_step=on_step,
        )
    )


@mcp.tool()
async def search_documents(
    query: str,
    top_k: int = config.search_tools.retrieval.top_k_rerank,
    title: str | None = None,
    description: str | None = None,
    source_path: str | None = None,
    tags: list[str] | None = None,
    ctx: Context | None = None,
) -> dict:
    """Search the RAG knowledge base and return the most relevant document
    chunks, ranked by a cross-encoder reranker. Query can be in Russian or
    English. Use this when you want the raw source excerpts yourself rather
    than a synthesized answer.

    Args:
        query: natural-language search query.
        top_k: how many reranked chunks to return.
        title: exact-match filter to a single page's title, if known.
        description: exact-match filter to a single page's front-matter
            description, if known.
        source_path: exact-match filter to a single page's path (as shown in
            results' source_path field), if known.
        tags: exact-match filter to chunks having any of these tags, if
            known.

    Returns a dict:
        results: list of chunk dicts (title, description, source_path,
            heading, text, retrieval_score, rerank_score).
        trace: list of {"step", "duration_ms"} for each pipeline stage
            (embed_query, qdrant_search, rerank), in call order.
        trace_id: hex OTel trace ID for this call (find it in Tempo/Loki),
            or null if the monitoring stack isn't up.

    Streams an MCP progress notification the instant each pipeline stage
    (embed_query/qdrant_search/rerank) finishes -- duration in ms plus a
    small characteristics dict (dim/candidate counts, never chunk text or
    scores) -- for clients that display live tool progress.
    """
    with tracer.start_as_current_span("search_documents") as span:
        span.set_attribute("rag.query", query)
        span.set_attribute("rag.top_k", top_k)
        timer = StepTimer()
        on_step = _make_progress_reporter(ctx)
        search_query = await _maybe_rewrite_query(query, timer, on_step)
        route_result: dict = {}
        try:
            results = await anyio.to_thread.run_sync(
                functools.partial(
                    retrieve,
                    search_query,
                    config.search_tools.retrieval.top_k_retrieve,
                    max(1, top_k),
                    timer,
                    qdrant_client=qdrant_client,
                    collection=QDRANT_COLLECTION,
                    embed_fn=_embed_fn,
                    reranker_enabled=config.search_tools.reranker.enabled,
                    rerank_fn=reranker_client.rerank if reranker_client else None,
                    rerank_confidence_cutoff=config.search_tools.reranker.confidence_cutoff,
                    title=title,
                    description=description,
                    source_path=source_path,
                    tags=tags,
                    max_chunks_per_source=config.search_tools.retrieval.max_chunks_per_source,
                    typo_vocab=_vocab_watcher.get() if config.search_tools.typo_correction.enabled else None,
                    typo_threshold=config.search_tools.typo_correction.threshold,
                    synonym_table=_synonym_table if config.search_tools.synonym_expansion.enabled else None,
                    query_router=_query_router,
                    route_result=route_result,
                    on_step=on_step,
                )
            )
            results = results + await _maybe_summary_search(route_result, timer, on_step)
        except Exception:
            _requests_counter.add(1, {"tool": "search_documents", "status": "error"})
            raise
        span.set_attribute("rag.chunks_returned", len(results))
        status = retrieval_status(
            results,
            reranker_enabled=config.search_tools.reranker.enabled,
            rerank_confidence_cutoff=config.search_tools.reranker.confidence_cutoff,
        )
        logger.info(
            "search_documents query=%r top_k=%d chunks=%d status=%s selected=%s",
            query,
            top_k,
            len(results),
            status,
            [
                {
                    "title": c["title"],
                    "source_path": c["source_path"],
                    "heading": c["heading"],
                    "retrieval_score": c["retrieval_score"],
                    "rerank_score": c["rerank_score"],
                }
                for c in results
            ],
        )
        _requests_counter.add(1, {"tool": "search_documents", "status": "ok"})
        trace_id = format_trace_id(span.get_span_context().trace_id) if OTEL_EXPORTER_OTLP_ENDPOINT else None
        return {"results": results, "trace": timer.trace, "trace_id": trace_id}


@mcp.tool()
async def answer_question(
    query: str,
    top_k: int = config.search_tools.retrieval.top_k_rerank,
    title: str | None = None,
    description: str | None = None,
    source_path: str | None = None,
    tags: list[str] | None = None,
    ctx: Context | None = None,
) -> dict:
    """Answer a question by retrieving relevant chunks from the RAG knowledge
    base, reranking them, and asking the AI_GATEWAY_REASONING_MODEL to
    compose an answer grounded in that context. Query can be in Russian or
    English; the answer is returned in the same language as the query.

    The reasoning behind the answer is already produced by the pipeline (see
    config.yml's search_tools.generation) -- callers should present
    `reasoning`/`trace` as-is rather than re-deriving them.

    Args:
        query: the user's question.
        top_k: how many reranked chunks to use as context.
        title: exact-match filter to a single page's title, if known.
        description: exact-match filter to a single page's front-matter
            description, if known.
        source_path: exact-match filter to a single page's path (as shown in
            context's source_path field), if known.
        tags: exact-match filter to chunks having any of these tags, if
            known.

    Returns a dict:
        answer: the answer text, or a "not found" message if no chunks
            matched.
        reasoning: the model's stated reasoning for the answer, or null if
            no chunks matched or the model didn't follow the expected
            format.
        sources: list of source strings the model cited, or [].
        context: the chunk dicts (same shape as search_documents' `results`)
            the answer was grounded in -- the local data behind `reasoning`.
        input_tokens: tokens consumed by the generate step, or null if no
            chunks matched. Always 0 -- token accounting isn't tracked for
            this backend, by design.
        output_tokens: same as input_tokens, for the generated reply.
        trace: list of {"step", "duration_ms"} for each pipeline stage
            (embed_query, qdrant_search, rerank, generate), in call order.
        trace_id: hex OTel trace ID for this call (find it in Tempo/Loki),
            or null if the monitoring stack isn't up.

    Streams an MCP progress notification the instant each pipeline stage
    (embed_query/qdrant_search/rerank/generate) finishes -- duration in ms
    plus a small characteristics dict (dim/candidate counts/answer length,
    never chunk text or the answer itself) -- for clients that display live
    tool progress.
    """
    with tracer.start_as_current_span("answer_question") as span:
        span.set_attribute("rag.query", query)
        span.set_attribute("rag.top_k", top_k)
        trace_id = format_trace_id(span.get_span_context().trace_id) if OTEL_EXPORTER_OTLP_ENDPOINT else None
        timer = StepTimer()
        on_step = _make_progress_reporter(ctx)
        search_query = await _maybe_rewrite_query(query, timer, on_step)
        route_result: dict = {}
        try:
            chunks = await anyio.to_thread.run_sync(
                functools.partial(
                    retrieve,
                    search_query,
                    config.search_tools.retrieval.top_k_retrieve,
                    max(1, top_k),
                    timer,
                    qdrant_client=qdrant_client,
                    collection=QDRANT_COLLECTION,
                    embed_fn=_embed_fn,
                    reranker_enabled=config.search_tools.reranker.enabled,
                    rerank_fn=reranker_client.rerank if reranker_client else None,
                    rerank_confidence_cutoff=config.search_tools.reranker.confidence_cutoff,
                    title=title,
                    description=description,
                    source_path=source_path,
                    tags=tags,
                    max_chunks_per_source=config.search_tools.retrieval.max_chunks_per_source,
                    typo_vocab=_vocab_watcher.get() if config.search_tools.typo_correction.enabled else None,
                    typo_threshold=config.search_tools.typo_correction.threshold,
                    synonym_table=_synonym_table if config.search_tools.synonym_expansion.enabled else None,
                    query_router=_query_router,
                    route_result=route_result,
                    on_step=on_step,
                )
            )
            summary_hits = await _maybe_summary_search(route_result, timer, on_step)
        except Exception:
            _requests_counter.add(1, {"tool": "answer_question", "status": "error"})
            raise
        span.set_attribute("rag.chunks_returned", len(chunks))
        span.set_attribute("rag.summary_hits", len(summary_hits))

        if not chunks and not summary_hits:
            logger.info("answer_question query=%r status=empty -- no chunks matched", query)
            _requests_counter.add(1, {"tool": "answer_question", "status": "no_chunks"})
            return {
                "answer": "No relevant documents were found in the knowledge base for this query.",
                "reasoning": None,
                "sources": [],
                "context": [],
                "input_tokens": None,
                "output_tokens": None,
                "trace": timer.trace,
                "trace_id": trace_id,
            }

        # Summary hits (only present for "global"-classified queries) are
        # appended after the normal chunks and share the same 1-based id
        # space -- generation cites by position across both, and
        # `context_items` (not just `chunks`) is what source_ids index into
        # below. A distinct source_template variant marks them as whole-page
        # summaries rather than chunk excerpts.
        context_items = chunks + summary_hits
        context = "\n\n".join(
            (config.search_tools.summary.source_template if c.get("kind") == "summary" else config.search_tools.source_template).format(
                id=i, title=c["title"], path=c["source_path"], updated=c["updated"] or "unknown", text=c["text"]
            )
            for i, c in enumerate(context_items, start=1)
        )
        profile = config.search_tools.generation
        user_content = profile.user_prompt_template.format(context=context, query=query)

        with timer("generate"):
            span.set_attribute("rag.model", config.search_tools.reasoning.model)
            result = await anyio.to_thread.run_sync(
                functools.partial(
                    reasoning_client.generate,
                    config.search_tools.reasoning.model,
                    profile.system_prompt,
                    user_content,
                    options=profile.options,
                    response_schema=profile.response_schema,
                )
            )
        on_step("generate", timer.trace[-1]["duration_ms"], {"answer_chars": len(result.text)})

        # response_schema-constrained JSON reply (see above) -- sources come
        # back as bare integer ids, mapped to citation strings here using
        # `chunks`, which the model never sees directly (see
        # parse_structured_answer's docstring).
        reasoning, answer, source_ids = parse_structured_answer(result.text)
        sources = [
            f'[{sid}] {context_items[sid - 1]["title"]} ({context_items[sid - 1]["source_path"]})'
            for sid in source_ids
            if 1 <= sid <= len(context_items)
        ]
        span.set_attribute("rag.input_tokens", result.input_tokens)
        span.set_attribute("rag.output_tokens", result.output_tokens)
        status = retrieval_status(
            chunks,
            reranker_enabled=config.search_tools.reranker.enabled,
            rerank_confidence_cutoff=config.search_tools.reranker.confidence_cutoff,
        )
        logger.info(
            "answer_question query=%r model=%s chunks=%d status=%s selected=%s",
            query,
            config.search_tools.reasoning.model,
            len(chunks),
            status,
            [
                {
                    "title": c["title"],
                    "source_path": c["source_path"],
                    "heading": c["heading"],
                    "retrieval_score": c["retrieval_score"],
                    "rerank_score": c["rerank_score"],
                }
                for c in chunks
            ],
        )
        # Raw pre-parse text -- lets you see exactly what the model said
        # even when parse_structured_answer() falls back to reasoning=None
        # because the reply wasn't valid JSON or was missing "answer".
        logger.info("answer_question query=%r raw_generation=%s", query, result.text)
        logger.info(
            "answer_question query=%r input_tokens=%d output_tokens=%d reasoning=%s sources=%s\nanswer=%s",
            query,
            result.input_tokens,
            result.output_tokens,
            reasoning,
            sources,
            answer,
        )
        _requests_counter.add(1, {"tool": "answer_question", "status": "ok"})
        return {
            "answer": answer,
            "reasoning": reasoning,
            "sources": sources,
            "context": context_items,
            "input_tokens": result.input_tokens,
            "output_tokens": result.output_tokens,
            "trace": timer.trace,
            "trace_id": trace_id,
        }


# ASGI app entry point, so the container can run this via a plain `uvicorn
# src.server:app` (needed for --reload in dev; mcp.run() drives its own
# uvicorn instance internally and doesn't support that).
app = mcp.streamable_http_app()

if __name__ == "__main__":
    mcp.run(transport="streamable-http")
