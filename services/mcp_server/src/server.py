"""MCP server exposing retrieval-augmented search/answering over the Qdrant
collection populated by ../ingest (services/ingest).

Pipeline for both tools:
  1. embed the query with MODEL_EMBED (same model used at ingest time,
     so query and document vectors live in the same space)
  2. fetch TOP_K_RETRIEVE nearest chunks from Qdrant
  3. rerank those candidates via the standalone reranker HTTP service (see
     /services/reranker) and keep the top TOP_K_RERANK -- skippable via
     config.yml's reranker.enabled
  4. `answer_question` additionally feeds the reranked chunks as context to a
     generation backend and returns its generated answer. The backend is
     selected by config.yml's backend.type:
       - "" : generation is skipped -- answer_question returns retrieved
         context only, same as search_documents.
       - "ollama" (default): the local MODEL_INSTRUCT_INTERNAL via Ollama's
         /api/chat.
       - "anthropic": the Claude API (MODEL_INSTRUCT_EXTERNAL, e.g.
         claude-sonnet-5) via the official `anthropic` SDK, using
         ANTHROPIC_API_KEY (the one credential that stays in the
         environment, not config.yml). Thinking is left off (system prompt
         already asks for a direct, ungrounded-guess-free answer) so latency
         stays comparable to the Ollama path.

Both tools return structured JSON rather than plain text: the retrieved
chunks (the local data the reasoning is grounded in), the reasoning itself
(`answer_question` only -- see config.yml's system_prompt for the Reasoning/
Answer/Sources format, which every generation backend follows since it's
shared), and a `trace` of pipeline steps with wall-clock timings. This is
meant to be consumed by a thin presentation layer (e.g. a Haiku-model agent)
rather than reasoned about further -- the reasoning already happened here.

Exposed over streamable-http so it can be added as a remote MCP connector in
both Open WebUI and Claude (Console / Desktop / claude.ai).

This file is handlers + wiring only. Everything else -- config schema,
retrieval, timing, answer parsing, and the Ollama/Anthropic backend calls --
lives in libs/. Env vars and config.yml are read *only* here; every libs/
function takes what it needs as arguments instead of reading globals itself.
"""

from __future__ import annotations

import functools
from pathlib import Path

from environs import Env
from mcp.server.fastmcp import FastMCP
from qdrant_client import QdrantClient

from _common.logging_config import configure_logging, get_logger
from _common.ollama import build_client, embed_query

from .libs import anthropic as anthropic_lib
from .libs import ollama as ollama_lib
from .libs.config import MCPServerConfig, load_config, parse_structured_answer
from .libs.reranker import RerankerClient
from .libs.retrieval import retrieve
from .libs.timer import StepTimer

# env.str(name)/env.int(name) raise a clear environs.EnvError if the
# variable is unset -- no hand-rolled required-env-var helper (that used to
# be copy-pasted between this file and services/ingest/ingest.py).
env = Env()

QDRANT_URL = env.str("QDRANT_URL")
QDRANT_COLLECTION = env.str("QDRANT_COLLECTION")
OLLAMA_BASE_URL = env.str("OLLAMA_BASE_URL")
MODEL_EMBED = env.str("MODEL_EMBED")
MCP_HOST = env.str("MCP_HOST")
MCP_PORT = env.int("MCP_PORT")
# Generation model, one env var per backend.type: MODEL_INSTRUCT_INTERNAL
# (Ollama) is required; MODEL_INSTRUCT_EXTERNAL (Anthropic) is only required
# when backend.type is "anthropic" -- BackendConfig's model_validator
# enforces that once the right one is picked and injected below.
MODEL_INSTRUCT_INTERNAL = env.str("MODEL_INSTRUCT_INTERNAL")
MODEL_INSTRUCT_EXTERNAL = env.str("MODEL_INSTRUCT_EXTERNAL", default=None)

configure_logging()
logger = get_logger(__name__)

# RAG-pipeline tuning knobs (reranker on/off, retrieval top-k, generation
# backend.type/max_tokens, system prompt) live in config.yml, not the
# environment -- see that file for the full field reference. It's
# bind-mounted alongside this package, so editing it just needs a restart,
# not a rebuild. backend.model isn't in config.yml at all -- it's picked
# from MODEL_INSTRUCT_INTERNAL/MODEL_INSTRUCT_EXTERNAL by backend.type here
# before validating. load_config()/.validate() report/exit on a missing
# file or a schema violation themselves, so there's nothing to catch here.
raw_config = load_config(Path("config.yml"))
backend_type = raw_config.raw.get("backend", {}).get("type")
raw_config.raw["backend"]["model"] = (
    MODEL_INSTRUCT_EXTERNAL if backend_type == "anthropic" else MODEL_INSTRUCT_INTERNAL
)
config: MCPServerConfig = raw_config.validate(MCPServerConfig)

# ANTHROPIC_API_KEY is the one credential that stays in the environment
# instead of config.yml. Only required (and only read) when backend.type is
# "anthropic" -- an ollama-only deployment doesn't need it set at all.
ANTHROPIC_API_KEY = env.str("ANTHROPIC_API_KEY") if config.backend.type == "anthropic" else None

# RERANKER_URL (a full base URL, e.g. "http://reranker:50051") points at the
# standalone reranker HTTP service -- only read when reranker.enabled is
# true in config.yml. The model/max_length that service runs live in *its
# own* config.yml, not here.
RERANKER_URL = env.str("RERANKER_URL") if config.reranker.enabled else None

qdrant = QdrantClient(url=QDRANT_URL)
ollama_client = build_client(OLLAMA_BASE_URL, config.ollama_timeout)
anthropic_client = anthropic_lib.build_client(ANTHROPIC_API_KEY) if ANTHROPIC_API_KEY else None
reranker_client = RerankerClient(RERANKER_URL) if RERANKER_URL else None

# Bound once so handlers don't need to know which embedding provider is
# configured -- currently always Ollama, but this is the seam if that ever
# changes.
_embed_fn = functools.partial(embed_query, ollama_client, MODEL_EMBED)

mcp = FastMCP("rag", host=MCP_HOST, port=MCP_PORT)


@mcp.tool()
def search_documents(
    query: str,
    top_k: int = config.retrieval.top_k_rerank,
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
    """
    timer = StepTimer()
    results = retrieve(
        query,
        config.retrieval.top_k_retrieve,
        max(1, top_k),
        timer,
        qdrant=qdrant,
        collection=QDRANT_COLLECTION,
        embed_fn=_embed_fn,
        reranker_enabled=config.reranker.enabled,
        rerank_fn=reranker_client.rerank if reranker_client else None,
        rerank_confidence_cutoff=config.reranker.confidence_cutoff,
        title=title,
        description=description,
        source_path=source_path,
    )
    return {"results": results, "trace": timer.trace}


@mcp.tool()
def answer_question(
    query: str,
    top_k: int = config.retrieval.top_k_rerank,
    title: str | None = None,
    description: str | None = None,
    source_path: str | None = None,
) -> dict:
    """Answer a question by retrieving relevant chunks from the RAG knowledge
    base, reranking them, and -- if config.yml's backend.type is set --
    asking that generation backend (local Ollama, or the Claude API) to
    compose an answer grounded in that context. Query can be in Russian or
    English; the answer is returned in the same language as the query.

    If backend.type is empty (""), no generation happens: this behaves like
    search_documents, returning only context/trace with answer/reasoning/
    sources left null/empty -- use this mode to skip generation latency/cost
    when you just want the retrieved chunks via this tool's response shape.

    The reasoning behind the answer (when generation runs) is already
    produced by the pipeline (see config.yml's system_prompt) -- callers
    should present `reasoning`/`trace` as-is rather than re-deriving them.

    Args:
        query: the user's question.
        top_k: how many reranked chunks to use as context.
        title: exact-match filter to a single page's title, if known.
        description: exact-match filter to a single page's front-matter
            description, if known.
        source_path: exact-match filter to a single page's path (as shown in
            context's source_path field), if known.

    Returns a dict:
        answer: the answer text; a "not found" message if no chunks matched;
            or null if backend.type is "" (generation skipped).
        reasoning: the model's stated reasoning for the answer, or null if
            no chunks matched, generation was skipped, or the model didn't
            follow the expected format.
        sources: list of source strings the model cited, or [].
        context: the chunk dicts (same shape as search_documents' `results`)
            the answer was grounded in -- the local data behind `reasoning`.
        trace: list of {"step", "duration_ms"} for each pipeline stage
            (embed_query, qdrant_search, rerank, generate), in call order.
            "generate" is absent when backend.type is "".
    """
    timer = StepTimer()
    chunks = retrieve(
        query,
        config.retrieval.top_k_retrieve,
        max(1, top_k),
        timer,
        qdrant=qdrant,
        collection=QDRANT_COLLECTION,
        embed_fn=_embed_fn,
        reranker_enabled=config.reranker.enabled,
        rerank_fn=reranker_client.rerank if reranker_client else None,
        rerank_confidence_cutoff=config.reranker.confidence_cutoff,
        title=title,
        description=description,
        source_path=source_path,
    )
    if not chunks:
        return {
            "answer": "No relevant documents were found in the knowledge base for this query.",
            "reasoning": None,
            "sources": [],
            "context": [],
            "trace": timer.trace,
        }

    if not config.backend.type:
        return {
            "answer": None,
            "reasoning": None,
            "sources": [],
            "context": chunks,
            "trace": timer.trace,
        }

    context = "\n\n".join(
        config.source_template.format(
            id=i, title=c["title"], path=c["source_path"], updated=c["updated"] or "unknown", text=c["text"]
        )
        for i, c in enumerate(chunks, start=1)
    )
    user_content = config.user_prompt_template.format(context=context, query=query)

    with timer("generate"):
        match config.backend.type:
            case "anthropic":
                # Guaranteed non-None by this point: ANTHROPIC_API_KEY is
                # required (so anthropic_client is built) whenever
                # backend.type is "anthropic", and BackendConfig's
                # model_validator requires model/max_tokens in that case too
                # -- mypy can't see either invariant from here.
                assert anthropic_client is not None
                assert config.backend.model is not None
                assert config.backend.max_tokens is not None
                raw = anthropic_lib.generate(
                    anthropic_client,
                    config.backend.model,
                    config.backend.max_tokens,
                    config.system_prompt,
                    user_content,
                )
            case "ollama":
                # MODEL_INSTRUCT_INTERNAL is a required env var (server.py
                # module scope), always injected into backend.model
                # regardless of type -- guaranteed non-None here too.
                assert config.backend.model is not None
                raw = ollama_lib.generate(ollama_client, config.backend.model, config.system_prompt, user_content, config.generate_timeout)
            case _:
                # Unreachable: config.backend.type is a pydantic Literal
                # ("", "ollama", "anthropic"), and "" already returned above.
                raise AssertionError(f"unhandled backend.type: {config.backend.type!r}")

    reasoning, answer, sources = parse_structured_answer(raw)
    return {
        "answer": answer,
        "reasoning": reasoning,
        "sources": sources,
        "context": chunks,
        "trace": timer.trace,
    }


# ASGI app entry point, so the container can run this via a plain `uvicorn
# src.server:app` (needed for --reload in dev; mcp.run() drives its own
# uvicorn instance internally and doesn't support that).
app = mcp.streamable_http_app()

if __name__ == "__main__":
    mcp.run(transport="streamable-http")
