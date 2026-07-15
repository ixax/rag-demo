# AGENTS.md

## What this repo is

A local RAG (Retrieval-Augmented Generation) foundation over the docs in `./content` (~260 Hugo-flavored markdown files: front matter, `{{< shortcode >}}` blocks, `<map>`/`<area>` HTML overlays).

Current state: infra skeleton, ingestion pipeline, and a query-time MCP server are all implemented.

## Stack

- `qdrant` — vector DB, ports 6333 (REST) / 6334 (gRPC).
- `ollama` — runs two CPU-sized local models (configurable via `.env`):
  - `MODEL_INSTRUCT_INTERNAL` (no default, e.g. `gemma3:4b`) — chat/generation when `mcp-server`'s `search_tools.backend.type` is `ollama`. Required in that case; unset it entirely for `anthropic_token`/`anthropic_subscription`/`""` search_tools.backend.type, since none of those need a local reasoning model. Uses `search_tools.generation_profiles.local` (`config.yml`) for its prompt/sampler options/response schema — see that key's comments.
  - `MODEL_EMBED` (default `embeddinggemma:300m`) — embedding, used by both `ingest` and `mcp-server` (same model both sides so query/document vectors are comparable). See README's "Embedding model" section for benchmarked alternatives.
- `ollama-pull` — one-shot init container that pulls both models on `make up`, then exits (exit code 0 is success, not a failure). Entrypoint script lives in [`services/ollama-pull/entrypoint.sh`](./services/ollama-pull/entrypoint.sh) (bind-mounted, not inline `command:` in `docker-compose.yml`) -- skips the `MODEL_INSTRUCT_INTERNAL` pull instead of failing when it's unset.
- `open-webui` — chat frontend at `http://localhost:${OPEN_WEBUI_PORT}` (default 3000), wired to Ollama via `OLLAMA_BASE_URL` and to `pipelines` via `OPENAI_API_BASE_URLS`/`OPENAI_API_KEYS`.
- `pipelines` — Open WebUI Pipelines runtime (`http://localhost:${PIPELINES_PORT}`, default 9099), loads [`services/open_webui_pipelines/rag_pipeline.py`](./services/open_webui_pipelines/rag_pipeline.py) as a model plugin. That file is a plain MCP client (using the `mcp` package) against `mcp-server`'s `answer_question` tool -- it registers as its own selectable model ("RAG (UE Docs)") in Open WebUI, separate from the Ollama chat models.
- `ingest` — one-shot, profile-gated (`--profile ingest`) job; see [`services/ingest/ingest.py`](./services/ingest/ingest.py) module docstring for the chunking/manifest design.
- `reranker` — always-on FastAPI/HTTP service (port `RERANKER_PORT_HOST`/50051 on the host, internal-only in normal use) exposing cross-encoder reranking (`POST /rerank`) via [`services/reranker/src/server.py`](./services/reranker/src/server.py). Model is `MODEL_RERANKER` (default `BAAI/bge-reranker-v2-m3`), downloaded from Hugging Face on first request into the `reranker_cache` volume, not baked into the image. `max_length` lives in `services/reranker/config.yml`. Kept as its own service (root build context + `services/reranker/Dockerfile`, sharing `services/_common`) so `mcp-server`'s image doesn't need `sentence-transformers`/`torch`, and so it can restart/rebuild independently. Only called when `mcp-server`'s `search_tools.reranker.enabled` is `true`.
- Observability stack (`tempo`, `loki`, `otel-collector`, `prometheus`, `cadvisor`, `node-exporter`, `grafana`) — all profile-gated (`profiles: ["monitoring"]`), started with `make monitoring-up`, never by plain `make up`. Traces/logs flow from instrumented services ([`services/_common/tracing.py`](./services/_common/tracing.py)) through `otel-collector` to Tempo (traces) and Loki (logs, correlated by `trace_id`); `cadvisor`/`node-exporter` feed Prometheus container/host metrics; Grafana (anonymous viewer access) has Tempo/Loki/Prometheus pre-provisioned as datasources plus one dashboard (`services/observability/grafana/dashboards/host-containers.json`). Core services degrade gracefully with this stack down -- OTLP export is best-effort, not a hard dependency.
- `mcp-server` — always-on MCP server (streamable HTTP, `/mcp` path, port `MCP_SERVER_PORT`/8000) exposing `search_documents`, `answer_question`, and (when `anthropic_chat.enabled` is true) `anthropic_chat` tools; see [`services/mcp_server/src/server.py`](./services/mcp_server/src/server.py) module docstring for the retrieve → rerank → generate pipeline. `server.py` is handlers + wiring only -- config schema, retrieval, timing, answer parsing, and the Ollama/Anthropic backend calls live in [`services/mcp_server/src/libs/`](./services/mcp_server/src/libs) (`common.py` shared, `ollama.py`/`anthropic.py`/`claude_cli.py` backend-specific); those functions take what they need as arguments rather than reading env/config themselves. `answer_question`'s generation model is `MODEL_INSTRUCT_INTERNAL` (`search_tools.backend.type: ollama`) or `MODEL_INSTRUCT_EXTERNAL` (`search_tools.backend.type: anthropic_token`/`anthropic_subscription`), picked at startup by `config.yml`'s `search_tools.backend.type`. `anthropic_chat` is a separate, retrieval-free tool with its own max_tokens/auth in `config.yml`'s top-level `anthropic_chat` section (independent of `search_tools`) and its own model from `MODEL_CHAT` (independent of `MODEL_INSTRUCT_EXTERNAL`).
- **Two Claude auth modes**, selected per-use (`search_tools.backend.type` for `answer_question`, `anthropic_chat.auth` for the chat tool), both in `services/mcp_server/config.yml`: `anthropic_token`/`"token"` calls the Messages API via the `anthropic` SDK (`libs/anthropic.py`), using `ANTHROPIC_API_KEY`, billed per token. `anthropic_subscription`/`"subscription"` shells out to the headless `claude` CLI as a subprocess instead (`libs/claude_cli.py`), using `CLAUDE_CODE_OAUTH_TOKEN` (a long-lived token from `claude setup-token`) -- billed against a Claude subscription. This is a different mechanism, not just a different credential on the same SDK call: the Messages API itself has no subscription-billed mode regardless of credential type, only the actual Claude Code client's usage is metered against a subscription (see `libs/claude_cli.py`'s docstring). `mcp-server`'s image installs Node.js + `@anthropic-ai/claude-code` for this (see its Dockerfile) -- unconditionally, since `config.yml` is read at container runtime, not build time. Both `max_tokens` fields are ignored in subscription mode (the CLI doesn't expose that control); use `generate_timeout`/`anthropic_chat.timeout` instead. See README's "Claude subscription auth" section for setup.

## Operating the stack

- `make up` / `make down` / `make restart`
- `make up-gpu` — same as `make up` but reserves the host's NVIDIA GPU for `ollama` (see [`docker-compose.gpu.yml`](./docker-compose.gpu.yml)); not applicable on macOS.
- `make status` (alias `make ps`) — container status **plus** a real HTTP health check against each service's port. Trust this over `docker compose ps` alone.
- `make logs` / `make mcp-logs` / `make reranker-logs` — tail all service logs, or just `mcp-server`/`reranker` (useful during their first-run downloads).
- `make pull-models` — re-run model pulling after changing `.env` model names.
- `make monitoring-up` / `make monitoring-down` / `make monitoring-logs` — start/stop/tail the profile-gated observability stack independently of the core stack.
- `make clean` — **destructive**, wipes all volumes (Qdrant data, pulled models, Open WebUI accounts, and monitoring's trace/log/metric data if that profile was ever started).

See README's [Stopping the stack](./README.md#stopping-the-stack) and [Troubleshooting](./README.md#troubleshooting) sections for the full operator-facing reference.

Infra/credentials config lives in `.env` (gitignored; copy from `.env.example`). `MODEL_EMBED` and `QDRANT_COLLECTION` have no fallback in `docker-compose.yml` -- `make up`/`make ingest` fail fast if either is unset. `MODEL_INSTRUCT_INTERNAL` is required only when `search_tools.backend.type: ollama` (`mcp-server` fails fast itself in that case, not compose). Pipeline tuning knobs (reranker, top-k, generation backend, chunking, `anthropic_chat`, etc.) live in `services/mcp_server/config.yml` and `services/ingest/config.yml` instead -- see README's Configuration section.

## Dev-loop convention for ASGI/uvicorn services

For any service in this repo backed by an ASGI app (currently just `mcp-server`, via `src.server:app` in [`services/mcp_server/src/server.py`](./services/mcp_server/src/server.py)):

- The Dockerfile uses `CMD` (not `ENTRYPOINT`), running the app through plain `uvicorn module:app`, so `docker-compose.yml`'s `command:` can override it freely (e.g. add `--reload`) without touching the image.
- The compose service bind-mounts its own source directory into the container (`./services/mcp_server:/app`), so edited source is visible inside the container without a rebuild.
- Together, that means dev iteration is just: edit the file on the host, then run the overridden command with `--reload` (uvicorn watches and restarts the app on change) instead of `make restart` / rebuilding the image. Reinstalling dependencies still needs a rebuild — the bind mount only shadows source files, not installed packages.

Apply the same pattern to any future ASGI-backed service added here.

## Logging

`ingest`, `mcp-server`, and `reranker` all log via stdlib `logging`, configured once by [`services/_common/logging_config.py`](./services/_common/logging_config.py) (`configure_logging()` + `get_logger(__name__)`) -- no `print()` calls in service code. `LOG_LEVEL` (default `INFO`) is read only inside that module, not by each service's entrypoint, since the setup is identical across all three.

## Git safety

No `git commit`, `push`, `pull`, `merge`, `rebase`, or any other git operation that changes repo/branch state -- including from a subagent spawned to do something else (e.g. a verification/test agent) -- without an explicit, direct instruction from the user for that specific action. Finding or fixing a bug during an unrelated task (e.g. an e2e test run) is not itself authorization to commit it; report the fix and let the user decide.

## Docs conventions

Don't write comparison/rationale prose in README/AGENTS/config comments (e.g. "chosen over X because Y", "Z was dropped since..."). State the current facts/config only -- no justification for alternatives not taken.

## Known quirks / gotchas

- **Port conflicts are host-specific.** `.env.example` defaults `OPEN_WEBUI_PORT` to 3000, but that's just a default — if it's taken on a given machine (observed once here due to an unrelated local SSH tunnel), the fix is changing `.env`, not the compose file.
- **The embed model is embedding-only.** If `MODEL_EMBED` is ever selected as the *chat* model in Open WebUI's model picker, every message fails with a 400 from Ollama's `/api/chat` (does not support chat). Always chat against `MODEL_INSTRUCT_INTERNAL`; reserve the embed model for embedding calls only.
- **Open WebUI's `/workspace/models` page** lists model *entries* you've explicitly created/pinned there — it's expected to be empty even when Ollama has models pulled. The actual pulled models show up in the chat page's model dropdown instead.
- Ollama's healthcheck is `ollama list` (not curl — the image has no curl/wget). Qdrant/Open WebUI health checks in the Makefile use `curl` from the host; `mcp-server`'s uses a raw TCP check (`/dev/tcp`) since an unauthenticated GET against a streamable-HTTP MCP endpoint returns 406, not 200. `make status` requires `curl` and `bash` locally.
- **`mcp-server`'s reranker (`MODEL_RERANKER`, default `BAAI/bge-reranker-v2-m3`) is downloaded from Hugging Face on first use**, not baked into the image — first `search_documents`/`answer_question` call after a fresh `make up` will be slow (and needs internet access from inside the container) while it downloads into the `reranker_cache` volume. Subsequent calls reuse the cached weights.
- **`pipelines` installs `rag_pipeline.py`'s `requirements:` frontmatter packages (the `mcp` package) into its own venv on container start**, not baked into the image — first `make up` (or any restart after editing that frontmatter) needs internet access from inside the container and takes a bit before the "RAG (UE Docs)" model appears in Open WebUI's picker. If it's missing, check `docker compose logs pipelines`.
- **A brand-new Open WebUI account needs an admin login before "RAG (UE Docs)" shows up in the model picker** — same first-visit admin account creation as any other model there; the `OPENAI_API_BASE_URLS`/`OPENAI_API_KEYS` wiring only registers the connection, Open WebUI still discovers its models (here, just the one from `rag_pipeline.py`) at runtime.
- FastMCP's DNS-rebinding protection (`TransportSecuritySettings`) only auto-enables when `host` is `127.0.0.1`/`localhost`/`::1`. `mcp-server` binds `0.0.0.0` (it needs to be reachable from other containers and external clients), so that protection stays off by design — do not "fix" this by changing the bind host without adding an explicit `allowed_hosts`/`allowed_origins` allowlist instead.
