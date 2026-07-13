# AGENTS.md

## What this repo is

A local RAG (Retrieval-Augmented Generation) foundation over the docs in `./content` (~260 Hugo-flavored markdown files: front matter, `{{< shortcode >}}` blocks, `<map>`/`<area>` HTML overlays).

Current state: infra skeleton, ingestion pipeline, and a query-time MCP server are all implemented.

## Stack

- `qdrant` — vector DB, ports 6333 (REST) / 6334 (gRPC).
- `ollama` — runs two CPU-sized local models (configurable via `.env`):
  - `OLLAMA_INSTRUCT_MODEL` (default `llama3.2:3b`) — chat/generation, used by `mcp-server`.
  - `OLLAMA_EMBED_MODEL` (default `bge-m3`) — embedding, used by both `ingest` and `mcp-server` (same model both sides so query/document vectors are comparable).
- `ollama-pull` — one-shot init container that pulls both models on `make up`, then exits (exit code 0 is success, not a failure).
- `open-webui` — chat frontend at `http://localhost:${OPEN_WEBUI_PORT}` (default 3000), wired to Ollama via `OLLAMA_BASE_URL`.
- `ingest` — one-shot, profile-gated (`--profile ingest`) job; see [`ingest/ingest.py`](./ingest/ingest.py) module docstring for the chunking/manifest design.
- `mcp-server` — always-on MCP server (streamable HTTP, `/mcp` path, port `MCP_SERVER_PORT`/8000) exposing `search_documents` and `answer_question` tools; see [`mcp_server/server.py`](./mcp_server/server.py) module docstring for the retrieve → rerank → generate pipeline.

Chosen over LibreChat (heavier: needs MongoDB+MeiliSearch) and Jan.ai (desktop-only, not compose-deployable).

## Operating the stack

- `make up` / `make down` / `make restart`
- `make status` (alias `make ps`) — container status **plus** a real HTTP health check against each service's port. Trust this over `docker compose ps` alone.
- `make logs` — tail all service logs.
- `make pull-models` — re-run model pulling after changing `.env` model names.
- `make clean` — **destructive**, wipes all volumes (Qdrant data, pulled models, Open WebUI accounts).

Config lives in `.env` (gitignored; copy from `.env.example`).

## Dev-loop convention for ASGI/uvicorn services

For any service in this repo backed by an ASGI app (currently just `mcp-server`, via `server:app` in [`mcp_server/server.py`](./mcp_server/server.py)):

- The Dockerfile uses `CMD` (not `ENTRYPOINT`), running the app through plain `uvicorn module:app`, so `docker-compose.yml`'s `command:` can override it freely (e.g. add `--reload`) without touching the image.
- The compose service bind-mounts its own source directory into the container (`./mcp_server:/app`), so edited source is visible inside the container without a rebuild.
- Together, that means dev iteration is just: edit the file on the host, then run the overridden command with `--reload` (uvicorn watches and restarts the app on change) instead of `make restart` / rebuilding the image. Reinstalling dependencies still needs a rebuild — the bind mount only shadows source files, not installed packages.

Apply the same pattern to any future ASGI-backed service added here.

## Known quirks / gotchas

- **Port conflicts are host-specific.** `.env.example` defaults `OPEN_WEBUI_PORT` to 3000, but that's just a default — if it's taken on a given machine (observed once here due to an unrelated local SSH tunnel), the fix is changing `.env`, not the compose file.
- **The embed model is embedding-only.** If `OLLAMA_EMBED_MODEL` is ever selected as the *chat* model in Open WebUI's model picker, every message fails with a 400 from Ollama's `/api/chat` (does not support chat). Always chat against `OLLAMA_INSTRUCT_MODEL`; reserve the embed model for embedding calls only.
- **Open WebUI's `/workspace/models` page** lists model *entries* you've explicitly created/pinned there — it's expected to be empty even when Ollama has models pulled. The actual pulled models show up in the chat page's model dropdown instead.
- Ollama's healthcheck is `ollama list` (not curl — the image has no curl/wget). Qdrant/Open WebUI health checks in the Makefile use `curl` from the host; `mcp-server`'s uses a raw TCP check (`/dev/tcp`) since an unauthenticated GET against a streamable-HTTP MCP endpoint returns 406, not 200. `make status` requires `curl` and `bash` locally.
- **`mcp-server`'s reranker (`RERANKER_MODEL`, default `BAAI/bge-reranker-v2-m3`) is downloaded from Hugging Face on first use**, not baked into the image — first `search_documents`/`answer_question` call after a fresh `make up` will be slow (and needs internet access from inside the container) while it downloads into the `reranker_cache` volume. Subsequent calls reuse the cached weights.
- FastMCP's DNS-rebinding protection (`TransportSecuritySettings`) only auto-enables when `host` is `127.0.0.1`/`localhost`/`::1`. `mcp-server` binds `0.0.0.0` (it needs to be reachable from other containers and external clients), so that protection stays off by design — do not "fix" this by changing the bind host without adding an explicit `allowed_hosts`/`allowed_origins` allowlist instead.
