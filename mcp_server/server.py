"""MCP server exposing retrieval-augmented search/answering over the Qdrant
collection populated by ../ingest.

Pipeline for both tools:
  1. embed the query with OLLAMA_EMBED_MODEL (same model used at ingest time,
     so query and document vectors live in the same space)
  2. fetch TOP_K_RETRIEVE nearest chunks from Qdrant
  3. rerank those candidates with a dedicated cross-encoder (bge-reranker,
     multilingual -- handles Russian and English queries against the mixed
     -language content) and keep the top TOP_K_RERANK
  4. `answer_question` additionally feeds the reranked chunks to the Ollama
     instruct model as context and returns its generated answer

Exposed over streamable-http so it can be added as a remote MCP connector in
both Open WebUI and Claude (Console / Desktop / claude.ai).
"""

from __future__ import annotations

import os
import sys

import httpx
from mcp.server.fastmcp import FastMCP
from qdrant_client import QdrantClient
from sentence_transformers import CrossEncoder


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        print(f"missing required environment variable: {name}", file=sys.stderr)
        raise SystemExit(1)
    return value


QDRANT_URL = _require_env("QDRANT_URL")
QDRANT_COLLECTION = _require_env("QDRANT_COLLECTION")
OLLAMA_BASE_URL = _require_env("OLLAMA_BASE_URL")
OLLAMA_EMBED_MODEL = _require_env("OLLAMA_EMBED_MODEL")
OLLAMA_INSTRUCT_MODEL = _require_env("OLLAMA_INSTRUCT_MODEL")
RERANKER_MODEL = _require_env("RERANKER_MODEL")
TOP_K_RETRIEVE = int(_require_env("TOP_K_RETRIEVE"))
TOP_K_RERANK = int(_require_env("TOP_K_RERANK"))
MCP_HOST = _require_env("MCP_HOST")
MCP_PORT = int(_require_env("MCP_PORT"))

SYSTEM_PROMPT = (
    "You are a documentation assistant. Answer the user's question using ONLY "
    "the numbered context excerpts below -- never use outside knowledge and "
    "never guess. Always reply in the same language the question was asked in "
    "(the content may be in a different language than the question -- "
    "translate as needed).\n\n"
    "Before answering, judge whether the excerpts actually address the "
    "question. If none of them are relevant, or together they only cover "
    "part of the question, or you're not sure the answer is correct, do NOT "
    "attempt an answer -- explicitly say (in the question's language) that "
    "the knowledge base doesn't contain a reliable answer, and briefly say "
    "what the excerpts do cover instead, if anything. A clear 'I don't know' "
    "is always better than a plausible-sounding guess.\n\n"
    "Only when the excerpts do support an answer: give it, then list the "
    "sources you actually used as '[n] title (path)'. Do not list sources "
    "when you answered that you don't know."
)

qdrant = QdrantClient(url=QDRANT_URL)
ollama = httpx.Client(base_url=OLLAMA_BASE_URL, timeout=180.0)

# Loaded lazily on first use, not at import time, so the server starts (and
# the MCP connection handshake succeeds) even before the ~1GB reranker
# checkpoint has been downloaded/cached.
_reranker: CrossEncoder | None = None


def get_reranker() -> CrossEncoder:
    global _reranker
    if _reranker is None:
        print(f"loading reranker model {RERANKER_MODEL} ...", file=sys.stderr)
        _reranker = CrossEncoder(RERANKER_MODEL, max_length=512)
        print("reranker ready", file=sys.stderr)
    return _reranker


def embed_query(query: str) -> list[float]:
    resp = ollama.post(
        "/api/embeddings", json={"model": OLLAMA_EMBED_MODEL, "prompt": query}
    )
    resp.raise_for_status()
    return resp.json()["embedding"]


def retrieve(query: str, top_k_retrieve: int, top_k_rerank: int) -> list[dict]:
    vector = embed_query(query)
    hits = qdrant.search(
        collection_name=QDRANT_COLLECTION, query_vector=vector, limit=top_k_retrieve
    )
    if not hits:
        return []

    pairs = [(query, hit.payload.get("text", "")) for hit in hits]
    scores = get_reranker().predict(pairs)

    reranked = sorted(zip(hits, scores), key=lambda pair: pair[1], reverse=True)
    reranked = reranked[:top_k_rerank]

    results = []
    for hit, rerank_score in reranked:
        payload = hit.payload or {}
        results.append(
            {
                "title": payload.get("title"),
                "source_path": payload.get("source_path"),
                "heading": payload.get("heading"),
                "text": payload.get("text"),
                "retrieval_score": float(hit.score),
                "rerank_score": float(rerank_score),
            }
        )
    return results


mcp = FastMCP("rag", host=MCP_HOST, port=MCP_PORT)


@mcp.tool()
def search_documents(query: str, top_k: int = TOP_K_RERANK) -> list[dict]:
    """Search the RAG knowledge base and return the most relevant document
    chunks, ranked by a cross-encoder reranker. Query can be in Russian or
    English. Use this when you want the raw source excerpts yourself rather
    than a synthesized answer.

    Args:
        query: natural-language search query.
        top_k: how many reranked chunks to return.
    """
    return retrieve(query, TOP_K_RETRIEVE, max(1, top_k))


@mcp.tool()
def answer_question(query: str, top_k: int = TOP_K_RERANK) -> str:
    """Answer a question by retrieving relevant chunks from the RAG knowledge
    base, reranking them, and asking the local Ollama instruct model to
    compose an answer grounded in that context. Query can be in Russian or
    English; the answer is returned in the same language as the query.

    Args:
        query: the user's question.
        top_k: how many reranked chunks to use as context.
    """
    chunks = retrieve(query, TOP_K_RETRIEVE, max(1, top_k))
    if not chunks:
        return "No relevant documents were found in the knowledge base for this query."

    context = "\n\n".join(
        f"[{i}] {c['title']} ({c['source_path']})\n{c['text']}"
        for i, c in enumerate(chunks, start=1)
    )
    user_content = f"Context:\n{context}\n\nQuestion: {query}"

    resp = ollama.post(
        "/api/chat",
        json={
            "model": OLLAMA_INSTRUCT_MODEL,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_content},
            ],
            "stream": False,
        },
    )
    resp.raise_for_status()
    return resp.json()["message"]["content"]


# ASGI app entry point, so the container can run this via a plain `uvicorn
# server:app` (needed for --reload in dev; mcp.run() drives its own uvicorn
# instance internally and doesn't support that).
app = mcp.streamable_http_app()

if __name__ == "__main__":
    mcp.run(transport="streamable-http")
