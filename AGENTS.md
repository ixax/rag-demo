# AGENTS.md

## What this repo is

A local RAG (Retrieval-Augmented Generation) foundation over the docs in `./content` (~260 Hugo-flavored markdown files: front matter, `{{< shortcode >}}` blocks, `<map>`/`<area>` HTML overlays).

Current state: infra skeleton, ingestion pipeline, and a query-time MCP server are all implemented.

## Stack

User-facing service list, ports, defaults, and make targets: see README's [Service links](./README.md#service-links), [Configuration](./README.md#configuration), and [Make targets](./README.md#make-targets) sections. This section only covers what isn't there — code layout and architecture.

- `ollama` and `reranker` both live outside this repo's compose project entirely now, as an external deployment this repo doesn't own or manage. This repo sets `OLLAMA_HOST`/`OLLAMA_PORT`/`RERANKER_HOST` (`.env`, all required, no default) plus `RERANKER_PORT` (defaults to `50051`) to reach them.
- `answer_question`'s generation step is Ollama-only: `OLLAMA_REASONING_MODEL` (required) uses `search_tools.generation` (`config.yml`) for its prompt/sampler options/response schema. Generation always runs -- there is no toggle to skip it.
- `pipelines` — [`services/open_webui_pipelines/rag_pipeline.py`](./services/open_webui_pipelines/rag_pipeline.py) is a plain MCP client (`mcp` package) against `mcp-server`'s `answer_question` tool, registered in Open WebUI as an OpenAI-compatible model.
- `ingest` — see [`services/ingest/ingest.py`](./services/ingest/ingest.py) module docstring for the chunking/manifest design.
- Observability stack — instrumentation lives in [`services/_common/tracing.py`](./services/_common/tracing.py); OTLP export is best-effort, core services don't depend on it.
- `mcp-server` — see [`services/mcp_server/src/server.py`](./services/mcp_server/src/server.py) module docstring for the retrieve → rerank → generate pipeline. `server.py` is handlers + wiring only -- config schema, retrieval, timing, answer parsing, and the Ollama backend calls live in [`services/mcp_server/src/libs/`](./services/mcp_server/src/libs); those functions take what they need as arguments rather than reading env/config themselves.

## Operating the stack

See README's [Make targets](./README.md#make-targets), [Stopping the stack](./README.md#stopping-the-stack), and [Troubleshooting](./README.md#troubleshooting) sections for the operator-facing reference.

Infra/credentials config lives in `.env` (gitignored; copy from `.env.example`). Pipeline tuning knobs (reranker, top-k, generation backend, chunking, etc.) live in `services/mcp_server/config.yml` and `services/ingest/config.yml` instead -- see README's Configuration section.

## Dev-loop convention for ASGI/uvicorn services

For any service in this repo backed by an ASGI app (currently just `mcp-server`, via `src.server:app` in [`services/mcp_server/src/server.py`](./services/mcp_server/src/server.py)):

- The Dockerfile uses `CMD` (not `ENTRYPOINT`), running the app through plain `uvicorn module:app`, so `docker-compose.yml`'s `command:` can override it freely (e.g. add `--reload`) without touching the image.
- The compose service bind-mounts its own source directory into the container (`./services/mcp_server:/app`), so edited source is visible inside the container without a rebuild.
- Together, that means dev iteration is just: edit the file on the host, then run the overridden command with `--reload` (uvicorn watches and restarts the app on change) instead of `make restart` / rebuilding the image. Reinstalling dependencies still needs a rebuild — the bind mount only shadows source files, not installed packages.

Apply the same pattern to any future ASGI-backed service added here.

## Logging

`ingest` and `mcp-server` both log via stdlib `logging`, configured once by [`services/_common/logging_config.py`](./services/_common/logging_config.py) (`configure_logging()` + `get_logger(__name__)`) -- no `print()` calls in service code. `LOG_LEVEL` (default `INFO`) is read only inside that module, not by each service's entrypoint, since the setup is identical across both.

## Git safety

No `git commit`, `push`, `pull`, `merge`, `rebase`, or any other git operation that changes repo/branch state -- including from a subagent spawned to do something else (e.g. a verification/test agent) -- without an explicit, direct instruction from the user for that specific action. Finding or fixing a bug during an unrelated task (e.g. an e2e test run) is not itself authorization to commit it; report the fix and let the user decide.

## Docs conventions

Don't write comparison/rationale prose in README/AGENTS/config comments (e.g. "chosen over X because Y", "Z was dropped since..."). State the current facts/config only -- no justification for alternatives not taken.
