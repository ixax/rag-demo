"""MCP server exposing retrieval-augmented search/answering over the Qdrant
collection populated by ../ingest (services/ingest).

Pipeline for both tools:
  1. embed the query with OLLAMA_EMBEDDINGS_MODEL (same model used at ingest time,
     so query and document vectors live in the same space)
  2. fetch TOP_K_RETRIEVE nearest chunks from Qdrant
  3. rerank those candidates via an external reranker HTTP service this repo
     doesn't own, and keep the top TOP_K_RERANK -- skippable via
     config.yml's search_tools.reranker.enabled
  4. `answer_question` additionally feeds the reranked chunks as context to
     the local OLLAMA_REASONING_MODEL via Ollama's /api/chat and returns its
     generated answer. This step always runs for answer_question.

Both search_documents/answer_question return structured JSON rather than
plain text: the retrieved chunks (the local data the reasoning is grounded
in), the reasoning itself (`answer_question` only -- see config.yml's
search_tools.generation for the JSON-schema-constrained Reasoning/Answer/
Sources contract Ollama's output follows), and a `trace` of pipeline steps
with wall-clock timings. This is meant to be consumed by a thin presentation
layer (e.g. a Haiku-model agent) rather than reasoned about further -- the
reasoning already happened here.

Exposed over streamable-http so it can be added as a remote MCP connector in
both Open WebUI and Claude (Console / Desktop / claude.ai).

This file is handlers + wiring only. Everything else -- config schema,
retrieval, timing, answer parsing, and the Ollama backend calls -- lives in
libs/. Env vars and config.yml are read *only* here; every libs/ function
takes what it needs as arguments instead of reading globals itself.
"""

from __future__ import annotations

import functools
from pathlib import Path

from environs import Env
from mcp.server.fastmcp import FastMCP
from opentelemetry.trace import format_trace_id
from qdrant_client import QdrantClient

from _common.logging_config import configure_logging, get_logger
from _common.ollama import build_client, embed_query
from _common.tracing import OTEL_EXPORTER_OTLP_ENDPOINT, configure_tracing, get_meter, get_tracer

from .libs import ollama as ollama_lib
from .libs.config import MCPServerConfig, load_config, parse_structured_answer
from .libs.reranker import RerankerClient
from .libs.retrieval import retrieve
from .libs.timer import StepTimer

# env.str(name)/env.int(name) raise a clear environs.EnvError if the
# variable is unset -- no hand-rolled required-env-var helper (that used to
# be copy-pasted between this file and services/ingest/ingest.py).
env = Env()

raw_config = load_config(Path("config.yml"))

QDRANT_URL = env.str("QDRANT_URL")
QDRANT_COLLECTION = env.str("QDRANT_COLLECTION")
OLLAMA_BASE_URL = env.str("OLLAMA_BASE_URL")
OLLAMA_EMBEDDINGS_MODEL = env.str("OLLAMA_EMBEDDINGS_MODEL")
MCP_HOST = env.str("MCP_HOST")
MCP_PORT = env.int("MCP_PORT")
# Reasoning/generation model for answer_question -- always required, since
# generation always runs.
OLLAMA_REASONING_MODEL = env.str("OLLAMA_REASONING_MODEL")

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
# comes from OLLAMA_REASONING_MODEL, injected here before validating.
# load_config()/.validate() report/exit on a missing file or a schema
# violation themselves, so there's nothing to catch here.
raw_config.raw["search_tools"]["reasoning"]["model"] = OLLAMA_REASONING_MODEL
config: MCPServerConfig = raw_config.validate(MCPServerConfig)

# RERANKER_URL (a full base URL, e.g. "http://reranker:50051") points at the
# standalone reranker HTTP service -- only read when search_tools.reranker.enabled is
# true in config.yml. The model/max_length that service runs live in *its
# own* config.yml, not here.
RERANKER_URL = env.str("RERANKER_URL") if config.search_tools.reranker.enabled else None

qdrant = QdrantClient(url=QDRANT_URL)
ollama_client = build_client(OLLAMA_BASE_URL, config.search_tools.ollama_timeout)
reranker_client = RerankerClient(RERANKER_URL) if RERANKER_URL else None

# Bound once so handlers don't need to know which embedding provider is
# configured -- currently always Ollama, but this is the seam if that ever
# changes.
_embed_fn = functools.partial(embed_query, ollama_client, OLLAMA_EMBEDDINGS_MODEL)

mcp = FastMCP("rag", host=MCP_HOST, port=MCP_PORT)


@mcp.tool()
def search_documents(
    query: str,
    top_k: int = config.search_tools.retrieval.top_k_rerank,
    title: str | None = None,
    description: str | None = None,
    source_path: str | None = None,
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

    Returns a dict:
        results: list of chunk dicts (title, description, source_path,
            heading, text, retrieval_score, rerank_score).
        trace: list of {"step", "duration_ms"} for each pipeline stage
            (embed_query, qdrant_search, rerank), in call order.
        trace_id: hex OTel trace ID for this call (find it in Tempo/Loki),
            or null if the monitoring stack isn't up.
    """
    with tracer.start_as_current_span("search_documents") as span:
        span.set_attribute("rag.query", query)
        span.set_attribute("rag.top_k", top_k)
        timer = StepTimer()
        try:
            results = retrieve(
                query,
                config.search_tools.retrieval.top_k_retrieve,
                max(1, top_k),
                timer,
                qdrant=qdrant,
                collection=QDRANT_COLLECTION,
                embed_fn=_embed_fn,
                reranker_enabled=config.search_tools.reranker.enabled,
                rerank_fn=reranker_client.rerank if reranker_client else None,
                rerank_confidence_cutoff=config.search_tools.reranker.confidence_cutoff,
                title=title,
                description=description,
                source_path=source_path,
            )
        except Exception:
            _requests_counter.add(1, {"tool": "search_documents", "status": "error"})
            raise
        span.set_attribute("rag.chunks_returned", len(results))
        logger.info(
            "search_documents query=%r top_k=%d chunks=%d selected=%s",
            query,
            top_k,
            len(results),
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
def answer_question(
    query: str,
    top_k: int = config.search_tools.retrieval.top_k_rerank,
    title: str | None = None,
    description: str | None = None,
    source_path: str | None = None,
) -> dict:
    """Answer a question by retrieving relevant chunks from the RAG knowledge
    base, reranking them, and asking the local Ollama OLLAMA_REASONING_MODEL to
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
    """
    with tracer.start_as_current_span("answer_question") as span:
        span.set_attribute("rag.query", query)
        span.set_attribute("rag.top_k", top_k)
        trace_id = format_trace_id(span.get_span_context().trace_id) if OTEL_EXPORTER_OTLP_ENDPOINT else None
        timer = StepTimer()
        try:
            chunks = retrieve(
                query,
                config.search_tools.retrieval.top_k_retrieve,
                max(1, top_k),
                timer,
                qdrant=qdrant,
                collection=QDRANT_COLLECTION,
                embed_fn=_embed_fn,
                reranker_enabled=config.search_tools.reranker.enabled,
                rerank_fn=reranker_client.rerank if reranker_client else None,
                rerank_confidence_cutoff=config.search_tools.reranker.confidence_cutoff,
                title=title,
                description=description,
                source_path=source_path,
            )
        except Exception:
            _requests_counter.add(1, {"tool": "answer_question", "status": "error"})
            raise
        span.set_attribute("rag.chunks_returned", len(chunks))

        if not chunks:
            logger.info("answer_question query=%r -- no chunks matched", query)
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

        context = "\n\n".join(
            config.search_tools.source_template.format(
                id=i, title=c["title"], path=c["source_path"], updated=c["updated"] or "unknown", text=c["text"]
            )
            for i, c in enumerate(chunks, start=1)
        )
        profile = config.search_tools.generation
        user_content = profile.user_prompt_template.format(context=context, query=query)

        with timer("generate"):
            span.set_attribute("rag.model", config.search_tools.reasoning.model)
            result = ollama_lib.generate(
                ollama_client,
                config.search_tools.reasoning.model,
                profile.system_prompt,
                user_content,
                config.search_tools.generate_timeout,
                options=profile.options,
                response_schema=profile.response_schema,
            )

        # response_schema-constrained JSON reply (see above) -- sources come
        # back as bare integer ids, mapped to citation strings here using
        # `chunks`, which the model never sees directly (see
        # parse_structured_answer's docstring).
        reasoning, answer, source_ids = parse_structured_answer(result.text)
        sources = [
            f'[{sid}] {chunks[sid - 1]["title"]} ({chunks[sid - 1]["source_path"]})'
            for sid in source_ids
            if 1 <= sid <= len(chunks)
        ]
        span.set_attribute("rag.input_tokens", result.input_tokens)
        span.set_attribute("rag.output_tokens", result.output_tokens)
        logger.info(
            "answer_question query=%r model=%s chunks=%d selected=%s",
            query,
            config.search_tools.reasoning.model,
            len(chunks),
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
            "context": chunks,
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
