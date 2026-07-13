---
name: rag-content-qa
description: "Answers questions about the documentation indexed in this repo's RAG stack (the ./content docs, e.g. asset-pipeline, dds-converter, engineering, ue). Use when someone asks 'what does the docs say about X', 'search the docs/RAG for X', 'find where X is documented', 'что говорится в документации про X', 'поищи в RAG/базе про X', or any question whose answer likely lives in ./content rather than in code or memory. Requires the 'rag' MCP server (mcp-server) to be connected — if its tools aren't available, tell the user to run `make up` and approve the server via /mcp."
allowed-tools:
  - mcp__rag__search_documents
  - mcp__rag__answer_question
---

# RAG Content Q&A

This project runs a local RAG stack (Qdrant + Ollama + `mcp-server`, see [`AGENTS.md`](../../../AGENTS.md)) over the docs in `./content`. Two MCP tools are exposed by the `rag` server:

- `mcp__rag__answer_question(query, top_k)` — full pipeline (embed → retrieve → rerank → generate). Returns a generated answer with cited sources. Use this for most questions — it's the direct path to an answer.
- `mcp__rag__search_documents(query, top_k)` — returns only the raw reranked chunks (title, path, heading, text, scores), no generation. Use this when you need to inspect/quote the actual source text yourself, cross-reference multiple chunks, or when `answer_question`'s generated answer seems incomplete/wrong and you want to verify against raw context.

## How to use

1. Default to `answer_question` for a direct question. Pass the query in whatever language the user asked in (the underlying model replies in the same language, Russian or English both work).
2. If the answer cites sources, surface the cited paths/headings to the user so they can verify against `./content` directly.
3. If `answer_question` comes back thin, wrong, or you need more context than the top-k it used, fall back to `search_documents` with a higher `top_k` and reason over the raw chunks yourself.
4. If the MCP tools aren't in your toolset at all, the `rag` server from `.mcp.json` likely isn't connected yet this session — tell the user to run `/mcp` and approve it (or check `make status` / `docker compose up -d mcp-server` if the container isn't running).

Don't guess at content — if `search_documents`/`answer_question` return nothing relevant, say so rather than fabricating an answer from general knowledge.
