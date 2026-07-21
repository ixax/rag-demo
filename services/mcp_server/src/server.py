"""MCP server exposing retrieval-augmented search/answering over the Qdrant
collection populated by ../ingest (services/ingest).

The RAG chain itself (embed -> Qdrant search -> optional rerank ->
[answer_question only] generate) lives in libs/pipeline.py's RagPipeline --
one method per step, wired up once at startup by libs/wiring.py's
build_pipeline(). This file is handlers + wiring only: it builds a
PipelineState from each tool call's arguments, runs it through the
pipeline, and shapes the pipeline's final state into the tool's response
dict.

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

Env vars are read *only* here (plus libs/wiring.py's build_pipeline(), which
this file delegates client/config construction to); every libs/ function
takes what it needs as arguments instead of reading globals itself.
"""

from __future__ import annotations

from typing import Callable

import anyio
import anyio.from_thread
import anyio.to_thread
from environs import Env
from mcp.server.fastmcp import Context, FastMCP
from opentelemetry.trace import format_trace_id

from _common.logging_config import configure_logging, get_logger
from _common.tracing import OTEL_EXPORTER_OTLP_ENDPOINT, configure_tracing, get_meter, get_tracer

from .libs.pipeline_state import PipelineState
from .libs.retrieval import retrieval_status
from .libs.timer import StepTimer
from .libs.wiring import build_pipeline

env = Env()
MCP_HOST = env.str("MCP_HOST")
MCP_PORT = env.int("MCP_PORT")

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
pipeline, config, _vocab_watcher = build_pipeline()

mcp = FastMCP("rag", host=MCP_HOST, port=MCP_PORT)


def _make_progress_reporter(ctx: Context | None) -> Callable[[str, float, dict], None]:
    """Builds an `on_step` callback for pipeline.run(): reports an MCP
    progress notification the instant each pipeline stage finishes, instead
    of only after the whole tool call returns. Must be called from a worker
    thread (pipeline.run() blocks on network I/O, so callers run it via
    anyio.to_thread.run_sync) -- anyio.from_thread.run() hops back to the
    event loop thread to actually send the notification while that worker
    thread keeps running the next stage. A running step count is kept
    internally so `progress` climbs monotonically across every step,
    including the separate `generate` step in answer_question. `ctx` is
    None only in the unusual case FastMCP has no request context to inject
    -- on_step is then a no-op instead of erroring."""
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
        tags: exact-match filter to chunks having any of the given tags, if
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
        state = PipelineState(
            query=query,
            top_k_retrieve=config.search_tools.retrieval.top_k_retrieve,
            top_k_rerank=max(1, top_k),
            title=title,
            description=description,
            source_path=source_path,
            tags=tags,
            want_answer=False,
            timer=StepTimer(),
            on_step=_make_progress_reporter(ctx),
        )
        try:
            state = await anyio.to_thread.run_sync(pipeline.run, state)
        except Exception:
            _requests_counter.add(1, {"tool": "search_documents", "status": "error"})
            raise
        span.set_attribute("rag.chunks_returned", len(state.results))
        status = retrieval_status(
            state.results,
            reranker_enabled=config.search_tools.reranker.enabled,
            rerank_confidence_cutoff=config.search_tools.reranker.confidence_cutoff,
        )
        logger.info(
            "search_documents query=%r top_k=%d chunks=%d status=%s selected=%s",
            query,
            top_k,
            len(state.results),
            status,
            [
                {
                    "title": c["title"],
                    "source_path": c["source_path"],
                    "heading": c["heading"],
                    "retrieval_score": c["retrieval_score"],
                    "rerank_score": c["rerank_score"],
                }
                for c in state.results
            ],
        )
        _requests_counter.add(1, {"tool": "search_documents", "status": "ok"})
        trace_id = format_trace_id(span.get_span_context().trace_id) if OTEL_EXPORTER_OTLP_ENDPOINT else None
        return {"results": state.results, "trace": state.timer.trace, "trace_id": trace_id}


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
        tags: exact-match filter to chunks having any of the given tags, if
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
        state = PipelineState(
            query=query,
            top_k_retrieve=config.search_tools.retrieval.top_k_retrieve,
            top_k_rerank=max(1, top_k),
            title=title,
            description=description,
            source_path=source_path,
            tags=tags,
            want_answer=True,
            timer=StepTimer(),
            on_step=_make_progress_reporter(ctx),
        )
        try:
            state = await anyio.to_thread.run_sync(pipeline.run, state)
        except Exception:
            _requests_counter.add(1, {"tool": "answer_question", "status": "error"})
            raise
        span.set_attribute("rag.chunks_returned", len(state.results))
        span.set_attribute("rag.summary_hits", len(state.summary_hits))

        if not state.context_items:
            logger.info("answer_question query=%r status=empty -- no chunks matched", query)
            _requests_counter.add(1, {"tool": "answer_question", "status": "no_chunks"})
            return {
                "answer": state.answer,
                "reasoning": state.reasoning,
                "sources": state.sources,
                "context": [],
                "input_tokens": None,
                "output_tokens": None,
                "trace": state.timer.trace,
                "trace_id": trace_id,
            }

        # context_items non-empty means generate_answer ran, so both are set.
        assert state.input_tokens is not None
        assert state.output_tokens is not None
        span.set_attribute("rag.model", config.search_tools.reasoning.model)
        span.set_attribute("rag.input_tokens", state.input_tokens)
        span.set_attribute("rag.output_tokens", state.output_tokens)
        status = retrieval_status(
            state.results,
            reranker_enabled=config.search_tools.reranker.enabled,
            rerank_confidence_cutoff=config.search_tools.reranker.confidence_cutoff,
        )
        logger.info(
            "answer_question query=%r model=%s chunks=%d status=%s selected=%s",
            query,
            config.search_tools.reasoning.model,
            len(state.results),
            status,
            [
                {
                    "title": c["title"],
                    "source_path": c["source_path"],
                    "heading": c["heading"],
                    "retrieval_score": c["retrieval_score"],
                    "rerank_score": c["rerank_score"],
                }
                for c in state.results
            ],
        )
        # Raw pre-parse text -- lets you see exactly what the model said
        # even when parse_answer() falls back to reasoning=None because the
        # reply wasn't valid JSON or was missing "answer".
        logger.info("answer_question query=%r raw_generation=%s", query, state.generation_text)
        logger.info(
            "answer_question query=%r input_tokens=%d output_tokens=%d reasoning=%s sources=%s\nanswer=%s",
            query,
            state.input_tokens,
            state.output_tokens,
            state.reasoning,
            state.sources,
            state.answer,
        )
        _requests_counter.add(1, {"tool": "answer_question", "status": "ok"})
        return {
            "answer": state.answer,
            "reasoning": state.reasoning,
            "sources": state.sources,
            "context": state.context_items,
            "input_tokens": state.input_tokens,
            "output_tokens": state.output_tokens,
            "trace": state.timer.trace,
            "trace_id": trace_id,
        }


# ASGI app entry point, so the container can run this via a plain `uvicorn
# src.server:app` (needed for --reload in dev; mcp.run() drives its own
# uvicorn instance internally and doesn't support that).
app = mcp.streamable_http_app()

if __name__ == "__main__":
    mcp.run(transport="streamable-http")
