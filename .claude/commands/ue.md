---
description: "Answers questions about the documentation indexed in this repo's RAG stack (the ./content docs, e.g. asset-pipeline, dds-converter, engineering, ue). Requires the 'rag' MCP server (mcp-server) to be connected."
argument-hint: <question>
allowed-tools: mcp__rag__search_documents, mcp__rag__answer_question
model: haiku
---

This project runs a local RAG stack (Qdrant + Ollama/Claude + `mcp-server`) over the docs in `./content`. The `rag` MCP server does all the reasoning itself — `answer_question` returns structured JSON:

- `answer` — the answer text (or a "not found" message).
- `reasoning` — the pipeline's own stated reasoning for the answer (which excerpts were relevant, why), or `null`.
- `sources` — list of cited source strings, or `[]`.
- `context` — the raw chunk dicts (title, source_path, heading, text, scores) the answer was grounded in.
- `trace` — list of `{"step", "duration_ms"}` for each pipeline stage (embed_query, qdrant_search, rerank, generate).

`search_documents` returns `{"results": [...chunks...], "trace": [...]}` — no `answer`/`reasoning`, just retrieval.

**You are a presentation layer, not a second reasoning pass.** The reasoning already happened inside the MCP server (embed → retrieve → rerank → generate, with the generation model instructed to output its own Reasoning/Answer/Sources). Do not re-derive, second-guess, or reorder the reasoning yourself — relay what the tool returned.

## How to use

The user's question is: $ARGUMENTS

1. Call `mcp__rag__answer_question(query)` with that question, in whatever language it was asked in.
2. Present the response as:
   - The `answer` text.
   - A short "Reasoning" line: the `reasoning` field verbatim (skip this line if it's `null`).
   - A "Sources" list from `sources` (fall back to `context[].source_path`/`title` if `sources` is empty but `context` isn't).
   - A "Timings" line: each `trace` step and its `duration_ms`, plus the total.
3. If `answer` says the knowledge base doesn't know, relay that plainly — don't try to answer from your own knowledge instead.
4. Use `mcp__rag__search_documents` only if the user explicitly wants raw source chunks rather than a synthesized answer — present its `results`/`trace` the same way, minus the reasoning/answer/sources fields (there are none).
5. If the MCP tools aren't in your toolset at all, the `rag` server from `.mcp.json` likely isn't connected this session — tell the user to run `/mcp` and approve it (or check `make status` / `docker compose up -d mcp-server` if the container isn't running).
