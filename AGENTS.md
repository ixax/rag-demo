# AGENTS.md

## What this repo is

A local RAG (Retrieval-Augmented Generation) foundation over the docs in `./content` (~260 Hugo-flavored markdown files: front matter, `{{< shortcode >}}` blocks, `<map>`/`<area>` HTML overlays).

Current state: infra skeleton, ingestion pipeline, and a query-time MCP server are all implemented.

## Stack

User-facing service list, ports, defaults, and make targets: see README's [Service links](./README.md#service-links), [Configuration](./README.md#configuration), and [Make targets](./README.md#make-targets) sections. This section only covers what isn't there — code layout and architecture.

- `ollama` — `MODEL_INSTRUCT_INTERNAL` uses `search_tools.generation_profiles.local` (`config.yml`) for its prompt/sampler options/response schema, distinct from the `agentic` profile used by the Claude backends below — see that key's comments.
- `ollama-pull` — entrypoint script lives in [`services/ollama-pull/entrypoint.sh`](./services/ollama-pull/entrypoint.sh), bind-mounted rather than inlined as `command:` in `docker-compose.yml`.
- `pipelines` — [`services/open_webui_pipelines/rag_pipeline.py`](./services/open_webui_pipelines/rag_pipeline.py) is a plain MCP client (`mcp` package) against `mcp-server`'s `answer_question` tool, registered in Open WebUI as an OpenAI-compatible model.
- `ingest` — see [`services/ingest/ingest.py`](./services/ingest/ingest.py) module docstring for the chunking/manifest design.
- `reranker` — [`services/reranker/src/server.py`](./services/reranker/src/server.py). Kept as its own service (root build context + `services/reranker/Dockerfile`, sharing `services/_common`) so `mcp-server`'s image doesn't need `sentence-transformers`/`torch`.
- Observability stack — instrumentation lives in [`services/_common/tracing.py`](./services/_common/tracing.py); OTLP export is best-effort, core services don't depend on it.
- `mcp-server` — see [`services/mcp_server/src/server.py`](./services/mcp_server/src/server.py) module docstring for the retrieve → rerank → generate pipeline. `server.py` is handlers + wiring only -- config schema, retrieval, timing, answer parsing, and the Ollama/Anthropic backend calls live in [`services/mcp_server/src/libs/`](./services/mcp_server/src/libs) (`common.py` shared, `ollama.py`/`anthropic.py`/`claude_cli.py` backend-specific); those functions take what they need as arguments rather than reading env/config themselves.
- **Two Claude auth modes**, selected per-use (`search_tools.backend.type` for `answer_question`, `anthropic_chat.auth` for the chat tool), both in `services/mcp_server/config.yml`: `anthropic_token`/`"token"` calls the Messages API via the `anthropic` SDK ([`libs/anthropic.py`](./services/mcp_server/src/libs/anthropic.py)). `anthropic_subscription`/`"subscription"` shells out to the headless `claude` CLI as a subprocess instead ([`libs/claude_cli.py`](./services/mcp_server/src/libs/claude_cli.py)) -- a different mechanism, not just a different credential on the same SDK call, since the Messages API itself has no subscription-billed mode (see that file's docstring). See README's [Claude subscription auth](./README.md#claude-subscription-auth) section for setup/billing.

## Operating the stack

See README's [Make targets](./README.md#make-targets), [Stopping the stack](./README.md#stopping-the-stack), and [Troubleshooting](./README.md#troubleshooting) sections for the operator-facing reference.

Infra/credentials config lives in `.env` (gitignored; copy from `.env.example`). Pipeline tuning knobs (reranker, top-k, generation backend, chunking, `anthropic_chat`, etc.) live in `services/mcp_server/config.yml` and `services/ingest/config.yml` instead -- see README's Configuration section.

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
