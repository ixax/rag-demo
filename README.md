# RAG Stack

A local, Docker Compose-based foundation for a Retrieval-Augmented Generation (RAG) setup over the documentation in `./content`.

## Service links

Once `make up` has finished (plus Ollama and the reranker running externally -- see [Configuration](#configuration) for `OLLAMA_HOST`/`RERANKER_HOST`) and `make status` reports everything healthy:

| Service | URL                             | Purpose |
|---------|---------------------------------|---------|
| Open WebUI | http://localhost:4000           | Chat frontend |
| Qdrant dashboard | http://localhost:6333/dashboard | Browse collections, points, and payloads in the vector DB |
| Qdrant REST API | http://localhost:6333           | Raw API (used by `ingest`/`mcp-server`) |
| mcp-server | http://localhost:8000/mcp       | MCP endpoint (streamable HTTP) |
| Ollama API | http://localhost:11434          | Raw API (used by Open WebUI/`mcp-server`) -- runs outside this compose project |
| pipelines | http://localhost:9099           | Open WebUI Pipelines runtime (used by Open WebUI) |
| reranker | http://localhost:50051          | Cross-encoder reranking HTTP API (used by `mcp-server`; not user-facing) -- runs outside this compose project |

Ports above are the `.env.example` defaults; if you've overridden `*_PORT` in `.env`, substitute accordingly. The monitoring stack (Tempo/Loki/Prometheus/Grafana/etc.) is off by default and not part of `make up` — see [Observability / monitoring](#observability--monitoring-optional) below for its own service links.

- **[Qdrant](https://qdrant.tech/)** — vector database. Ships with a built-in web dashboard for browsing collections/points, served at `/dashboard` on its own REST port — no separate viewer service needed.
- **[Ollama](https://ollama.com/)** — runs two models (an instruct model for answer generation, an embedding model used at both ingest and query time). Lives entirely outside this compose project now, as its own standalone deployment this repo doesn't manage. This project only needs `OLLAMA_HOST`/`OLLAMA_PORT` in `.env` pointed at wherever it's running.
- **[Open WebUI](https://openwebui.com/)** — a simple, ChatGPT-like chat frontend wired to Ollama out of the box, plus the `pipelines` connection below.
- **`pipelines`** — [Open WebUI Pipelines](https://github.com/open-webui/pipelines) runtime, registered in Open WebUI as an OpenAI-compatible connection. Loads [`services/open_webui_pipelines/rag_pipeline.py`](./services/open_webui_pipelines/rag_pipeline.py), which shows up as its own selectable model, **RAG (UE Docs)**, in Open WebUI's model picker. Selecting it and asking a question calls `mcp-server`'s `answer_question` tool directly over MCP and returns its answer — no chat model or tool-calling involved.
- **`ingest`** — one-shot job that walks `./content`, chunks/cleans the Hugo markdown, embeds it, and upserts it into Qdrant. See [`services/ingest/`](./services/ingest).
- **reranker** — standalone FastAPI/HTTP service exposing cross-encoder reranking (`POST /rerank`). Like Ollama, lives entirely outside this compose project as its own standalone deployment this repo doesn't manage. This project only needs `RERANKER_HOST`/`RERANKER_PORT` in `.env` pointed at wherever it's running, plus `MODEL_RERANKER` set to whatever model that instance is actually serving. Called only when `mcp-server`'s `search_tools.reranker.enabled` is `true` (ships `false`).
- **`mcp-server`** — an MCP server exposing retrieval-augmented search/answering (embed query → Qdrant search → cross-encoder rerank → generation) over streamable HTTP, for use as a remote MCP connector from Open WebUI and Claude. Generation backend is Ollama, the Claude API (per-token), or the headless Claude Code CLI billed against a Claude subscription instead of tokens — see [Claude subscription auth](#claude-subscription-auth) below. Also exposes a standalone `anthropic_chat` tool (raw text in, raw Claude reply out, no retrieval) when enabled. See [`services/mcp_server/`](./services/mcp_server).

## Prerequisites

- Docker
- Docker Compose v2 (the `docker compose` CLI plugin)
- `make`
- `curl` (used by `make status` for health checks)

## Quick start

```bash
cp .env.example .env
```

Also bring up Ollama and the reranker -- a standalone deployment now, not part of this one -- unless you already have external instances of either to point `OLLAMA_HOST`/`RERANKER_HOST` (`.env`) at.

```bash
make up
make status
```

`.env.example` already ships working defaults for the two settings that have no fallback (`MODEL_EMBED=embeddinggemma:300m`, `QDRANT_COLLECTION=content`) and for `MODEL_RERANKER` — the copy above is enough to get this project's own stack running with no further edits (make sure `MODEL_EMBED`/`MODEL_RERANKER` match whatever your Ollama/reranker deployment is actually serving). `MODEL_INSTRUCT_INTERNAL`/`MODEL_INSTRUCT_EXTERNAL`/`MODEL_CHAT`/Claude credentials are left blank; whichever ones you need depend on `services/mcp_server/config.yml`'s `search_tools.backend.type` (ships as `ollama`, so out of the box you'd still need `MODEL_INSTRUCT_INTERNAL` set in `.env`, e.g. `gemma3:4b`, and pulled into your Ollama instance before `answer_question` can generate — `search_documents` and retrieval-only use work with no instruct model or Claude setup at all). Switching `backend.type` to `anthropic_token`/`anthropic_subscription` instead needs Claude credentials, see [Claude subscription auth](#claude-subscription-auth). See [Configuration](#configuration) for every combination.

`make status` polls `mcp-server`/Qdrant/`open-webui`/`pipelines` and reports whether they're actually responding, not just "container running" -- it doesn't check Ollama/reranker, since those live outside this compose project (check them via their own folders' `docker compose ps`).

Once everything is healthy, open **http://localhost:4000** to reach Open WebUI. On first visit you'll be asked to create a local admin account (this is Open WebUI's own auth, stored in its own volume — not an external account). The Ollama models configured below should already appear in the model picker.

To inspect what's actually in the vector DB (collections, points, payloads) after running `make ingest`, open **http://localhost:6333/dashboard** — Qdrant's built-in browsing UI, no extra service required.

## Configuration

Infra/credentials/deployment settings live in `.env` (copy it from `.env.example`); pipeline tuning knobs live in each service's own `config.yml` (see below) instead.

| Variable                  | Default                   | Purpose                                                                                                                                                                                                                                                                                                                                  |
|---------------------------|---------------------------|------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| `MODEL_EMBED`             | **required, no default**  | Embedding model pulled into Ollama; used by both `ingest` and `mcp-server` so query/document vectors share one space                                                                                                                                                                                                                     |
| `MODEL_RERANKER`          | `BAAI/bge-reranker-v2-m3` | Cross-encoder model for the `reranker` service, pulled from Hugging Face on first use                                                                                                                                                                                                                                                    |
| `MODEL_INSTRUCT_INTERNAL` |                           | Instruct model pulled into Ollama; used by `mcp-server` for answer generation when `search_tools.backend.type` is `ollama`. Required in that case (e.g. `gemma3:4b`), otherwise leave unset -- your Ollama deployment's pull step should skip pulling it instead of failing. Harmless to leave set when switching `backend.type` away from `ollama` -- it's simply unused by any other backend, no need to unset it |
| `MODEL_INSTRUCT_EXTERNAL` |                           | Claude model id for `mcp-server` answer generation when `search_tools.backend.type` is `anthropic_token` or `anthropic_subscription` (e.g. `claude-sonnet-5`); not an Ollama model, so `ollama-pull` doesn't touch it                                                                                                                    |
| `MODEL_CHAT`              |                           | Claude model id for the standalone `anthropic_chat` tool (e.g. `claude-sonnet-5`); required when `config.yml`'s `anthropic_chat.enabled` is true. Independent of `MODEL_INSTRUCT_EXTERNAL` -- the two tools can point at different Claude models                                                                                         |
| `QDRANT_COLLECTION`       | **required, no default**  | Qdrant collection used by both `ingest` and `mcp-server`                                                                                                                                                                                                                                                                                 |
| `QDRANT_HTTP_PORT`        | `6333`                    | Qdrant REST API port on the host                                                                                                                                                                                                                                                                                                         |
| `QDRANT_GRPC_PORT`        | `6334`                    | Qdrant gRPC port on the host                                                                                                                                                                                                                                                                                                             |
| `OLLAMA_HOST`              | `host.docker.internal`    | Host/IP other services connect to for Ollama, which runs as its own standalone project -- defaults to reaching it on the Docker host itself; point it at a different host/IP if it runs elsewhere                                                                              |
| `OLLAMA_PORT`             | `11434`                   | Port `OLLAMA_HOST` is reached on -- match whatever port your Ollama deployment publishes it as                                                                                                                                                                                                                                                  |
| `RERANKER_HOST`           | `host.docker.internal`    | Host/IP `mcp-server` connects to for reranking, which runs as its own standalone project -- defaults to reaching it on the Docker host itself; point it at a different host/IP if it runs elsewhere                                                                        |
| `RERANKER_PORT`           | `50051`                   | Port `RERANKER_HOST` is reached on -- match whatever port your reranker deployment publishes it as                                                                                                                                                                                                                                                |
| `OPEN_WEBUI_PORT`         | `3000`                    | Open WebUI port on the host                                                                                                                                                                                                                                                                                                              |
| `MCP_SERVER_PORT`         | `8000`                    | `mcp-server`'s streamable-HTTP port on the host                                                                                                                                                                                                                                                                                          |
| `PIPELINES_PORT`          | `9099`                    | `pipelines`'s HTTP port on the host                                                                                                                                                                                                                                                                                                      |
| `PIPELINES_API_KEY`       | `0p3n-w3bu!`              | Shared auth key `open-webui` and `pipelines` use to talk to each other over the OpenAI-compatible connection. Change it from the upstream default if `pipelines` is ever reachable from outside the Docker network                                                                                                                     |
| `FORCE_INGEST`            | `false`                   | Ingest run mode (see [Ingesting content](#ingesting-content)); a run-mode toggle, not a static setting, so it stays an env var                                                                                                                                                                                                           |
| `ANTHROPIC_API_KEY`       |                           | Required only when `search_tools.backend.type` is `anthropic_token`, or `anthropic_chat.auth` is `token` -- a credential, so it stays out of `config.yml`. Leave **unset** for `anthropic_subscription`/`auth: subscription` (uses `CLAUDE_CODE_OAUTH_TOKEN` below instead) -- see [Claude subscription auth](#claude-subscription-auth) |
| `CLAUDE_CODE_OAUTH_TOKEN` |                           | A long-lived token from `claude setup-token`. Required only when `search_tools.backend.type` is `anthropic_subscription`, or `anthropic_chat.auth` is `subscription` -- picked up by the `claude` CLI subprocess itself, not read by `mcp-server` -- see [Claude subscription auth](#claude-subscription-auth)                           |
| `LOG_LEVEL`               | `INFO`                    | Log level for `ingest`/`mcp-server`/`reranker` (`services/_common/logging_config.py`); one of Python logging's level names (`DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`)                                                                                                                                                             |
| `TEMPO_PORT`              | `3200`                    | Tempo's HTTP port on the host (traces) — only relevant with the `monitoring` profile up, see [Observability / monitoring](#observability--monitoring-optional)                                                                                                                                                                          |
| `LOKI_PORT`               | `3100`                    | Loki's HTTP port on the host (logs) — monitoring profile only                                                                                                                                                                                                                                                                            |
| `PROMETHEUS_PORT`         | `9090`                    | Prometheus's web UI port on the host — monitoring profile only                                                                                                                                                                                                                                                                           |
| `CADVISOR_PORT`           | `8085`                    | cAdvisor's web UI port on the host (per-container CPU/mem/network) — monitoring profile only                                                                                                                                                                                                                                             |
| `NODE_EXPORTER_PORT`      | `9101`                    | node-exporter's metrics port on the host (host-level CPU/mem/network/disk) — monitoring profile only                                                                                                                                                                                                                                     |
| `GRAFANA_PORT`            | `3001`                    | Grafana's web UI port on the host (dashboards over Tempo/Loki/Prometheus) — monitoring profile only                                                                                                                                                                                                                                       |

`MODEL_EMBED` and `QDRANT_COLLECTION` have no fallback in `docker-compose.yml` (`${VAR:?...}`) — `make up`/`make ingest` fail fast with a clear message if either is unset, rather than silently running against the wrong model/collection. `.env.example` ships working values for both; copy it to `.env` and you're covered. `MODEL_INSTRUCT_INTERNAL` is required only conditionally (see the table above) -- `mcp-server` itself fails fast at startup if it's unset while `search_tools.backend.type: ollama` is selected.

Both Ollama models are CPU-sized on purpose (3B instruct model, small embedding model) so the stack runs without a GPU. Swap them for larger models in `.env` if you have the hardware, then re-pull them in your Ollama deployment.

GPU support for Ollama, and re-pulling/swapping its models, is entirely that deployment's concern, not this repo's. The reranker always runs on CPU regardless.

### `config.yml` files

Both `ingest` and `mcp-server` are bind-mounted (`./services/ingest:/app`-equivalent via their build context, `./services/mcp_server:/app`), so editing a `config.yml` and restarting/rerunning the service picks up the change — no rebuild needed for `mcp-server` (bind-mounted at runtime); `ingest` rebuilds automatically every run (`make ingest`/`make ingest-force` always pass `--build`), so its `config.yml` edits apply on the next run without a separate step.

- **[`services/mcp_server/config.yml`](./services/mcp_server/config.yml)** — two top-level keys. `search_tools` (everything behind `search_documents`/`answer_question`): reranker on/off, `top_k_retrieve`/`top_k_rerank`, the generation `backend` (`""` to skip generation entirely, `"ollama"`, `"anthropic_token"`, or `"anthropic_subscription"` with `max_tokens`; its `model` comes from `MODEL_INSTRUCT_INTERNAL`/`MODEL_INSTRUCT_EXTERNAL`, see the table above), and `generation_profiles` -- prompts/sampler options keyed by model family: `agentic` (Claude/Codex-class, used by `anthropic_token`/`anthropic_subscription`) gets a long-form textual Reasoning/Answer/Sources contract, `local` (open-weight models via `ollama`, e.g. `gemma3:4b`) gets a short prompt plus a JSON schema (`response_schema`) that constrains Ollama's output at the sampler level instead of relying on the model following a textual format instruction. `anthropic_chat` (a separate, retrieval-free tool, independent of `search_tools`): `enabled`/`auth`/`max_tokens`/`system_prompt`; its `model` comes from `MODEL_CHAT`, not this file (see the table above and [Claude subscription auth](#claude-subscription-auth)). Every field has an explanatory comment in the file itself.
- **[`services/ingest/config.yml`](./services/ingest/config.yml)** — content/state paths, chunking (`max_chars`/`min_chars`), and `upsert_batch_size`.
- **reranker `config.yml`** — `max_length` (token truncation) lives in the reranker's own external deployment, not in this repo; the cross-encoder model itself is `MODEL_RERANKER` (see the table above).

### Claude subscription auth

`search_tools.backend.type: "anthropic_subscription"` (RAG answer generation) and `anthropic_chat.auth: "subscription"` (the standalone chat tool) both bill against a Claude Pro/Max/Team **subscription** instead of `ANTHROPIC_API_KEY`'s per-token balance.

**Why this isn't just "the SDK with a different credential":** the Claude Messages API (what the `anthropic` SDK/`ANTHROPIC_API_KEY` path calls) always bills against an organization's per-token API credit balance, no matter what kind of credential authenticates the request -- there's no request parameter or OAuth scope that makes a raw Messages API call draw from a chat subscription's usage instead. The only client whose usage is metered against a Pro/Max/Team subscription is the actual Claude Code client. So `anthropic_subscription`/`auth: "subscription"` don't call the SDK at all -- `mcp-server` shells out to the headless `claude` CLI (Claude Code's own non-interactive mode, `claude -p`) as a subprocess, and that binary's usage is what's billed against the subscription. See [`services/mcp_server/src/libs/claude_cli.py`](./services/mcp_server/src/libs/claude_cli.py) for the implementation.

Set either mode up in two steps:

1. **Generate a token**: run `claude setup-token` (requires the [Claude Code CLI](https://code.claude.com/docs) already installed and logged into an active Claude subscription on some machine -- it doesn't have to be this host). This requires an active Claude subscription and prints a long-lived token (`sk-ant-oat01-...`). Put it in `.env` as `CLAUDE_CODE_OAUTH_TOKEN` -- it's a secret, so treat it like `ANTHROPIC_API_KEY` (never commit it; `.env` is gitignored).

   This is the only supported way to get subscription-billed headless access: an interactive `claude login`'s session credentials are stored in the OS keychain (Keychain on macOS, similar on other platforms), which a Docker container can't read; `claude setup-token`'s output is a portable, container-friendly credential instead.
2. **Point `config.yml` at it**: set `search_tools.backend.type: "anthropic_subscription"` and/or `anthropic_chat: {enabled: true, auth: "subscription"}` in `services/mcp_server/config.yml`, then `make restart` (or just restart `mcp-server`) to pick it up. No other compose change is needed -- `docker-compose.yml` already passes `CLAUDE_CODE_OAUTH_TOKEN` through to the container (and the image already has the `claude` CLI installed, see `services/mcp_server/Dockerfile`).

`ANTHROPIC_API_KEY` and `CLAUDE_CODE_OAUTH_TOKEN` don't interact -- each is read by a different code path (the SDK vs. the CLI subprocess), so there's no shadowing concern like there would be between two credentials on the same client. `search_tools.backend.type` and `anthropic_chat.auth` are independent: you can mix, e.g. `search_tools.backend.type: "ollama"` for cheap RAG answers with `anthropic_chat.auth: "subscription"` for ad-hoc Claude chat, or put one on `anthropic_token` and the other on `anthropic_subscription`.

`max_tokens` doesn't apply to either subscription mode (`search_tools.backend.max_tokens` / `anthropic_chat.max_tokens`) -- the `claude` CLI doesn't expose that control. Use `search_tools.generate_timeout` / `anthropic_chat.timeout` instead to bound how long the subprocess can run.

`claude setup-token`'s token is long-lived but not eternal; if `mcp-server` starts logging authentication errors from the subscription path, re-run `claude setup-token` and update `CLAUDE_CODE_OAUTH_TOKEN` in `.env`.

## Embedding model

`MODEL_EMBED` candidates were benchmarked with [`.claude/agents/embed-model-bench.md`](./.claude/agents/embed-model-bench.md) — a Claude Code subagent with a fixed RU/EN documentation-style corpus baked into its own instructions (8 docs across 4 topics, 6 queries incl. cross-lingual, a long-text truncation probe) so every model is scored against identical input. Re-run it (via the `Agent` tool, `subagent_type: embed-model-bench`) to re-check a model after an Ollama update.

Current: `embeddinggemma:300m`. Also benchmarked:

| Model | Dim | Context | Latency (warm) | Top-1 / MRR | Params / quant | Result |
|---|---|---|---|---|---|---|
| `embeddinggemma:300m` | 768 | 2048 tokens | ~260-520ms | 6/6, 1.00 | 307.6M, BF16 | **Current.** Smallest candidate that clears the 512-token ceiling below; fastest of the models that passed every check. |
| `bge-m3` | 1024 | 8192 tokens | ~0.6-2.2s | 6/6, 1.00 | 567M, F16, ~1.2GB | Previous `MODEL_EMBED`. Same accuracy as `embeddinggemma:300m` but ~2x the params and slower per call. |
| `qwen3-embedding:0.6b` | 1024 | 32768 tokens | ~2-3.5s | 6/6, 1.00 | 595.8M, Q8_0 | Essentially the same size as `bge-m3`, and 6-10x slower per call than `embeddinggemma:300m` in steady state. |
| `qllama/multilingual-e5-base` | 768 | 512 tokens | ~0.5-0.7s | 6/6, 1.00 | 277M, Q8_0 | Fast, but same 512-token ceiling as the row below. |
| `granite-embedding:278m`, `paraphrase-multilingual` | 768 | 512 tokens | fast | 6/6, 1.00 | 278M | Error out (HTTP 500, not silent truncation) on inputs over ~512 tokens -- too small a context window for this repo's `chunking.max_chars: 1800`. |
| `nomic-embed-text-v2-moe`, `mxbai-embed-large` | — | 512 tokens | — | — | 475M, 335M | Same 512-token ceiling as above. |
| `multilingual-e5-small`, `multilingual-e5-base` (bare tags) | — | — | — | — | — | Not available on Ollama's registry under those bare tags -- Hugging Face names, not Ollama ones (see `qllama/multilingual-e5-base` above for the actual Ollama tag). |

Switching `MODEL_EMBED` requires a full `make ingest-force` — a different model means a different vector dimension, so existing points in the Qdrant collection aren't comparable to newly embedded queries.

## Make targets

| Target          | Description                                                                 |
|-----------------|-------------------------------------------------------------------------------|
| `make up`       | Start the stack in the background (`qdrant`/`mcp-server`/`open-webui`/pipelines only -- Ollama and the reranker are a separate, externally managed deployment) |
| `make down`     | Stop the stack (containers removed, volumes kept)                            |
| `make restart`  | `down` followed by `up`                                                      |
| `make status`   | Show container status plus a health check against each service's HTTP port   |
| `make ps`       | Alias for `make status`                                                      |
| `make logs`     | Tail logs from all services                                                  |
| `make mcp-logs` | Tail logs from `mcp-server` only |
| `make ingest`   | Run the ingest job once (incremental — only new/changed/removed files)       |
| `make ingest-force` | Run the ingest job with a full collection rebuild                       |
| `make monitoring-up` | Start the optional observability stack (Tempo/Loki/Prometheus/Grafana/cAdvisor/node-exporter) on top of the running stack — see [Observability / monitoring](#observability--monitoring-optional) |
| `make monitoring-down` | Stop the observability stack's containers (volumes kept)               |
| `make monitoring-logs` | Tail logs from the observability stack's containers                    |
| `make clean`    | **Destructive.** Stops the stack (including monitoring, if it was up) and deletes all volumes (Qdrant data, Open WebUI accounts/settings, ingest manifest, and monitoring's own trace/log/metric/dashboard data). Doesn't touch Ollama/reranker's own volumes -- see their own projects for that |

## Verifying the stack manually

```bash
# Containers healthy?
docker compose ps

# Models pulled into Ollama? (check on your Ollama deployment)
docker compose exec ollama ollama list

# Qdrant has data after `make ingest`?
curl http://localhost:6333/collections/content
```

## Ingesting content

`make ingest` walks `./content`, strips Hugo shortcodes/HTML, chunks each page by heading, embeds each chunk with `MODEL_EMBED`, and upserts into the `QDRANT_COLLECTION` collection. Re-running it only touches files that changed since the last run (tracked in a manifest); `make ingest-force` rebuilds the collection from scratch. See [`services/ingest/`](./services/ingest) for details.

`make ingest` (no force) auto-escalates to a full rebuild if `QDRANT_COLLECTION` doesn't exist yet or exists but has zero points, regardless of what the manifest says -- the manifest is keyed by file path, not by collection name, so switching `QDRANT_COLLECTION` without wiping the manifest would otherwise leave the new collection empty.

`make ingest-force` drops and recreates the collection (`recreate_collection` -- delete + create) the moment the first non-empty file is embedded, then upserts as it goes; it does not wait until every file has been processed. If literally every file is empty/draft, the existing collection is left untouched (neither wiped nor recreated).

Change detection (incremental mode) is per-file, not per-chunk: each file's SHA-256 (raw bytes) is compared against the hash stored for it in the manifest (`STATE_DIR/manifest.json`). A new file, or one whose hash no longer matches, has all of its existing Qdrant points deleted (matched by the `source_path` payload field) and every one of its chunks re-embedded and re-upserted -- there's no partial/paragraph-level diffing within a file. A file removed from `./content` has its points deleted and its manifest entry dropped. Files that didn't change are skipped entirely -- no delete, no re-embed, no re-upsert.

Progress is logged per file as `[i/N, X%]`, and both the manifest and any already-embedded points are saved incrementally as the run goes (not buffered until the end) -- an interrupted run keeps whatever it completed up to that point instead of losing the whole run.

## Observability / monitoring (optional)

Off by default. `make up`/`docker compose up` never starts these containers -- they're gated behind Compose's `monitoring` profile in `docker-compose.yml`. None of the core services (`ingest`/`mcp-server`, or the standalone reranker) fail or behave differently when the monitoring stack is down; they emit OTLP traces on a best-effort basis and simply have nowhere to send them until `otel-collector` is up.

```bash
make monitoring-up
```

| Service | URL | Purpose |
|---------|-----|---------|
| Grafana | http://localhost:3001 | Dashboards over the three stores below (anonymous viewer access enabled, no login needed) |
| Prometheus | http://localhost:9090 | Metrics store/query UI (container stats from cAdvisor, host stats from node-exporter, plus Tempo's span-metrics) |
| Tempo | http://localhost:3200 | Trace store (queried through Grafana, not usually opened directly) |
| Loki | http://localhost:3100 | Log store — retrieved-chunk/answer payloads correlated by `trace_id` (queried through Grafana) |
| cAdvisor | — | Per-container CPU/mem/network exporter, scraped by Prometheus (no useful standalone UI here) |
| node-exporter | — | Host-level CPU/mem/network/disk exporter, scraped by Prometheus |
| otel-collector | grpc :4317 / http :4318 | Receives OTLP from instrumented services and fans traces out to Tempo, logs out to Loki, metrics out on :8889 for Prometheus to scrape |

Ports above are the `.env.example` defaults (`TEMPO_PORT`/`LOKI_PORT`/`PROMETHEUS_PORT`/`CADVISOR_PORT`/`NODE_EXPORTER_PORT`/`GRAFANA_PORT`, see [Configuration](#configuration)). Open **http://localhost:3001** and use the pre-provisioned "Host & Containers" dashboard for a starting point; Tempo/Loki are also added as Grafana datasources automatically (`services/observability/grafana/provisioning`), so you can jump from a trace to its correlated logs.

Stop the monitoring stack independently of the core stack with `make monitoring-down` (containers stop, volumes/data kept); it comes back with its history intact via `make monitoring-up`. `make clean` wipes its volumes along with everything else's.

## Claude Code skills for maintaining Qdrant

This repo registers the [qdrant/skills](https://github.com/qdrant/skills) plugin (all 9 skills: search quality, monitoring, model migration, scaling, performance, deployment options, version upgrades, client SDKs, edge/embedded) via [`.claude/settings.json`](./.claude/settings.json):

```json
{
  "extraKnownMarketplaces": {
    "qdrant": { "source": { "source": "github", "repo": "qdrant/skills" } }
  },
  "enabledPlugins": {
    "qdrant@qdrant": true
  }
}
```

No files are vendored into the repo and no manual `/plugin` command is needed — this config is checked into git, so Claude Code picks it up and installs the marketplace/plugin automatically for anyone who opens the repo. Skills trigger automatically when a question matches their description; three worth knowing about for this project specifically:

- **`qdrant-search-quality`** — diagnosing bad retrieval results and hybrid-search strategies; relevant to tuning `TOP_K_RETRIEVE`/`TOP_K_RERANK` and the reranker in `mcp-server`.
- **`qdrant-model-migration`** — safely changing `MODEL_EMBED` without leaving stale/incompatible vectors in the `content` collection.
- **`qdrant-monitoring`** — metrics, health checks, and debugging beyond the basic TCP check in `make status`.

If a plugin install doesn't apply automatically in an existing session, open `/plugin` once (or restart Claude Code) to pick up the project settings.

## Connecting the MCP server

`mcp-server` speaks MCP over streamable HTTP at `http://<host>:${MCP_SERVER_PORT}/mcp` (default `http://localhost:8000/mcp`) and exposes two tools:

- `search_documents(query, top_k)` — returns the raw reranked source chunks (title, path, heading, text, scores).
- `answer_question(query, top_k)` — runs the full pipeline and returns a generated answer with cited sources.
- `anthropic_chat(prompt, system_prompt?)` — only registered when `services/mcp_server/config.yml`'s `anthropic_chat.enabled` is `true`. A separate, retrieval-free tool: raw text in, raw Claude reply out, no Qdrant/reranker involved. See [`config.yml` files](#configyml-files) above.

Queries can be in Russian or English; `answer_question` replies in whatever language the query was asked in. See [`services/mcp_server/`](./services/mcp_server) for the retrieval/rerank/generation pipeline details.

- **Claude (Console / Desktop / claude.ai):** add it as a remote MCP connector using the URL above (`http://localhost:8000/mcp` if running locally, or a reachable host/port otherwise).
- **Open WebUI:** add it under Settings → Tools as an MCP tool server using the same URL (from inside the compose network, other containers should reach it at `http://mcp-server:8000/mcp` instead of `localhost`).

## Stopping the stack

| Goal | Command | Effect |
|------|---------|--------|
| Stop everything, keep data | `make down` | Stops/removes containers; Qdrant data, Open WebUI accounts, and ingest manifest all survive in their volumes for the next `make up`. Doesn't touch Ollama/reranker -- they're separate projects, stop them from their own folders |
| Stop everything and start fresh | `make clean` | **Destructive.** `docker compose down -v` — same as above but also deletes every volume in this project (Qdrant data, Open WebUI accounts/settings, ingest manifest, and monitoring's trace/log/metric/dashboard data if that profile was ever started). Doesn't touch Ollama/reranker's own volumes |
| Restart everything | `make restart` | `make down` then `make up` — same data-keeping behavior as `make down` |
| Stop just the monitoring stack | `make monitoring-down` | Stops Tempo/Loki/Prometheus/Grafana/cAdvisor/node-exporter only; core services (`qdrant`/`open-webui`/`mcp-server`/`pipelines`) keep running |
| Stop one core service | `docker compose stop <service>` (e.g. `docker compose stop open-webui`) | Stops just that container; others keep running. `mcp-server`/`ingest` depend on `qdrant` being up, so stopping it breaks retrieval for the rest. Ollama/reranker live outside this compose project entirely (per `OLLAMA_HOST`/`RERANKER_HOST`) -- stopping either from its own folder just makes calls that need it fail at request time |
| Restart one core service after editing its `config.yml` | `docker compose restart <service>` (e.g. `docker compose restart mcp-server`) | `mcp-server`/`ingest` are bind-mounted (see [`config.yml` files](#configyml-files)), so this is enough to pick up an edit — no rebuild needed for `mcp-server`; `ingest` is one-shot and always rebuilds on its own `make ingest`/`make ingest-force` invocation. Same applies to the reranker on its own deployment |

`ingest` never needs "stopping" in the above sense — it's a one-shot job (`profiles: ["ingest"]`, `restart: "no"`) that exits on its own once a run finishes; it isn't part of the always-on stack `make up`/`make down` manage.

## Troubleshooting

- **A service in `make status` shows "NOT RESPONDING" right after `make up`.** Most services take a few seconds to a few minutes to become healthy on first start. Check `docker compose ps` for its actual state and `docker compose logs -f <service>` for what it's doing; re-run `make status` after it settles. See the specific cases below for the slow-first-start services.
- **Model pulling into Ollama is slow or seems stuck.** This is now your Ollama deployment's concern, not this repo's -- see its own troubleshooting docs.
- **First `search_documents`/`answer_question` call is slow.** The reranker's cross-encoder model (`MODEL_RERANKER`) downloads from Hugging Face on first use, not at build time -- see the reranker deployment's own troubleshooting docs.
- **"RAG (UE Docs)" model is missing from Open WebUI's picker.** Two possible causes: (1) `pipelines` installs `rag_pipeline.py`'s `requirements:` frontmatter packages into its own venv on every container start (not baked into the image) — check `docker compose logs pipelines` for install progress or errors; (2) a brand-new Open WebUI account needs to complete first-visit admin signup before any model (Ollama or pipelines) appears in the picker at all.
- **Open WebUI's `/workspace/models` page is empty even though models are pulled.** Expected — that page only lists model *entries* you've explicitly pinned there. The actual pulled Ollama models (and the pipelines model) show up in the chat page's model dropdown instead.
- **Chatting against the embedding model in Open WebUI fails with an HTTP 400.** `MODEL_EMBED` is embedding-only and doesn't support Ollama's `/api/chat` endpoint. Always chat against `MODEL_INSTRUCT_INTERNAL` (or a Claude model via `mcp-server`'s tools); reserve `MODEL_EMBED` for embedding calls.
- **A port is already in use on the host.** Port defaults in `.env.example` are dev-convenience values, not guarantees — if one collides with something else already running on your machine, change the corresponding `*_PORT` variable in `.env` (not the compose file) and `make restart`.
- **`make up`/`make ingest` fails immediately with a message about `MODEL_EMBED` or `QDRANT_COLLECTION` being unset.** These two have no fallback on purpose (`${VAR:?...}` in `docker-compose.yml`) — copy `.env.example` to `.env` if you haven't, or check you haven't accidentally deleted/commented out one of those two lines.
- **`mcp-server` fails at startup complaining about a missing model/credential.** Its generation backend is chosen by `services/mcp_server/config.yml`'s `search_tools.backend.type`; each type requires a different env var (`MODEL_INSTRUCT_INTERNAL` for `ollama`; `MODEL_INSTRUCT_EXTERNAL` + `ANTHROPIC_API_KEY` for `anthropic_token`; `MODEL_INSTRUCT_EXTERNAL` + `CLAUDE_CODE_OAUTH_TOKEN` for `anthropic_subscription`) — see [Configuration](#configuration) and [Claude subscription auth](#claude-subscription-auth). `mcp-server` fails fast rather than falling back silently, so the log line at `make mcp-logs` names exactly what's missing.
- **`mcp-server` logs authentication errors from the subscription backend after it worked before.** `claude setup-token`'s token is long-lived but not eternal — re-run `claude setup-token` and update `CLAUDE_CODE_OAUTH_TOKEN` in `.env`, then `make restart`.
- **Switched `MODEL_EMBED` and search results look wrong/empty.** Different embedding models produce vectors of different (and non-comparable) dimensions. A model swap always needs a full `make ingest-force`, not just `make ingest` — see [Embedding model](#embedding-model).
- **Traces/logs aren't showing up in Grafana.** The monitoring stack must be started separately with `make monitoring-up` — it's not part of `make up`. If it's already up, confirm `otel-collector` is healthy (`docker compose ps`) and that the service you're checking actually made a request recently (spans/logs are only emitted per-request, not continuously).
- **GPU isn't being used by Ollama.** GPU support is entirely that deployment's concern, not this repo's. Not supported on macOS at all (no GPU passthrough into Docker containers there). The reranker always runs on CPU regardless.
