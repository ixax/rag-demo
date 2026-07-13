# AGENTS.md

## What this repo is

A local RAG (Retrieval-Augmented Generation) foundation over the docs in `./content` (~260 Hugo-flavored markdown files: front matter, `{{< shortcode >}}` blocks, `<map>`/`<area>` HTML overlays).

Current state: **infra skeleton only** (docker-compose stack). Content ingestion (chunking, cleaning, embedding, upserting into Qdrant) and any query-time retrieval/agent/MCP wiring are **not implemented yet** — that's the next phase of work, kept deliberately separate from the skeleton.

## Stack

- `qdrant` — vector DB, ports 6333 (REST) / 6334 (gRPC).
- `ollama` — runs two CPU-sized local models (configurable via `.env`):
  - `OLLAMA_INSTRUCT_MODEL` (default `llama3.2:3b`) — for future content cleaning/normalization.
  - `OLLAMA_EMBED_MODEL` (default `nomic-embed-text`) — for future embedding.
- `ollama-pull` — one-shot init container that pulls both models on `make up`, then exits (exit code 0 is success, not a failure).
- `open-webui` — chat frontend at `http://localhost:${OPEN_WEBUI_PORT}` (default 3000), wired to Ollama via `OLLAMA_BASE_URL`.

Chosen over LibreChat (heavier: needs MongoDB+MeiliSearch) and Jan.ai (desktop-only, not compose-deployable).

## Operating the stack

- `make up` / `make down` / `make restart`
- `make status` (alias `make ps`) — container status **plus** a real HTTP health check against each service's port. Trust this over `docker compose ps` alone.
- `make logs` — tail all service logs.
- `make pull-models` — re-run model pulling after changing `.env` model names.
- `make clean` — **destructive**, wipes all volumes (Qdrant data, pulled models, Open WebUI accounts).

Config lives in `.env` (gitignored; copy from `.env.example`).

## Known quirks / gotchas

- **Port conflicts are host-specific.** `.env.example` defaults `OPEN_WEBUI_PORT` to 3000, but that's just a default — if it's taken on a given machine (observed once here due to an unrelated local SSH tunnel), the fix is changing `.env`, not the compose file.
- **`nomic-embed-text` is embedding-only.** If it's ever selected as the *chat* model in Open WebUI's model picker, every message fails with `"nomic-embed-text:latest" does not support chat` (400 from Ollama's `/api/chat`). Always chat against `llama3.2:3b` (or whatever `OLLAMA_INSTRUCT_MODEL` is set to); reserve the embed model for embedding calls only.
- **Open WebUI's `/workspace/models` page** lists model *entries* you've explicitly created/pinned there — it's expected to be empty even when Ollama has models pulled. The actual pulled models (`llama3.2:3b`, `nomic-embed-text`) show up in the chat page's model dropdown instead.
- Ollama's healthcheck is `ollama list` (not curl — the image has no curl/wget). Qdrant/Open WebUI health checks in the Makefile use `curl` from the host, so `make status` requires `curl` to be installed locally.

## Next phase (not started)

Ingestion pipeline: walk `./content`, strip Hugo shortcodes/HTML noise, chunk (header-aware + fixed-size), optionally clean with the instruct model, embed with the embed model, upsert into Qdrant with metadata (source path, title, chunk index). Deterministic point IDs (hash of path+chunk index) so re-running updates rather than duplicates. See the earlier plan discussion for the originally scoped design (custom Python service, `langchain-text-splitters` for chunking, `qdrant-client` for upserts) if picking this back up.
