# RAG Stack

A local, Docker Compose-based foundation for a Retrieval-Augmented Generation (RAG) setup over the documentation in `./content`.

## Service links

Once `make up` has finished (plus the AI gateway running externally -- see [Configuration](#configuration) for `AI_GATEWAY_HOST`) and `make status` reports everything healthy:

| Service          | URL                                        | Purpose                                                                                                                                       |
|------------------|--------------------------------------------|-----------------------------------------------------------------------------------------------------------------------------------------------|
| Qdrant dashboard | http://localhost:6333/dashboard            | Browse collections, points, and payloads in the vector DB                                                                                     |
| Qdrant REST API  | http://localhost:6333                      | Raw API (used by `ingest`/`mcp-server`)                                                                                                       |
| mcp-server       | http://localhost:8000/mcp                  | MCP endpoint (streamable HTTP)                                                                                                                |
| AI gateway       | http://<AI_GATEWAY_HOST>:<AI_GATEWAY_PORT> | Reasoning/embeddings (OpenAI-compatible API) and reranking (`/rerank`) (used by Open WebUI/`mcp-server`) -- runs outside this compose project |

`make up` only brings up `qdrant`/`mcp-server` -- that's enough to use `mcp-server` as a remote MCP connector from Claude or another MCP client. Open WebUI/`pipelines` are gated behind the `open-webui` Compose profile and need their own step, see [Open WebUI (optional)](#open-webui-optional) below for their service links and how to start them.

Ports above are the `.env.example` defaults; if you've overridden `*_PORT` in `.env`, substitute accordingly. The monitoring stack (Tempo/Loki/Prometheus/Grafana/etc.) is off by default and not part of `make up` — see [Observability / monitoring](#observability--monitoring-optional) below for its own service links.

- **[Qdrant](https://qdrant.tech/)** — vector database. Ships with a built-in web dashboard for browsing collections/points, served at `/dashboard` on its own REST port — no separate viewer service needed.
- **AI gateway** — fronts reasoning, embeddings, and reranking behind one host:port, routing each request by its `model` field to whatever it's actually configured to serve. Reasoning/embeddings speak the OpenAI-compatible API (`/v1/chat/completions`, `/v1/embeddings`); reranking speaks a Cohere/HuggingFace-style `/rerank` API. Lives entirely outside this compose project, as its own standalone deployment this repo doesn't manage. This project needs `AI_GATEWAY_HOST`/`AI_GATEWAY_PORT` in `.env` (host required, port defaulted) pointed at wherever it's running, plus one model alias each for `AI_GATEWAY_EMBEDDINGS_MODEL`/`AI_GATEWAY_REASONING_MODEL`/`AI_GATEWAY_RERANKER_MODEL` (all required, no default -- must match a `model_name` in that gateway's own config). Auth is gateway-specific, so `AI_GATEWAY_AUTH_HEADER`/`AI_GATEWAY_AUTH_VALUE_TEMPLATE` say which header carries `AI_GATEWAY_AUTH_KEY` and in what shape -- both are required, no default, since there's no header/template combination that fits every gateway. Examples:
  - LiteLLM, default virtual-key auth: `AI_GATEWAY_AUTH_HEADER=Authorization`, `AI_GATEWAY_AUTH_VALUE_TEMPLATE=Bearer {key}`
  - LiteLLM behind a custom header: `AI_GATEWAY_AUTH_HEADER=x-litellm-api-key`, `AI_GATEWAY_AUTH_VALUE_TEMPLATE=Bearer {key}`
  - Azure OpenAI: `AI_GATEWAY_AUTH_HEADER=api-key`, `AI_GATEWAY_AUTH_VALUE_TEMPLATE={key}`
  - Anthropic: `AI_GATEWAY_AUTH_HEADER=x-api-key`, `AI_GATEWAY_AUTH_VALUE_TEMPLATE={key}`
- **`ingest`** — one-shot job that walks `./content`, chunks/cleans the Hugo markdown, embeds it, and upserts it into Qdrant. See [`services/ingest/`](./services/ingest).
- **`mcp-server`** — an MCP server exposing retrieval-augmented search/answering (embed query → Qdrant search → cross-encoder rerank → generation) over streamable HTTP, for use as a remote MCP connector from Open WebUI and Claude. Reranking (`search_tools.reranker.enabled`, ships `false`) and generation (`AI_GATEWAY_REASONING_MODEL`, generation always runs for `answer_question`) both go through the AI gateway above. See [`services/mcp_server/`](./services/mcp_server).
- **[Open WebUI](https://openwebui.com/)** and **`pipelines`** — optional chat frontend, not required to use `mcp-server` directly. See [Open WebUI (optional)](#open-webui-optional).

## Prerequisites

- Docker
- Docker Compose v2 (the `docker compose` CLI plugin)
- `make`
- `curl` (used by `make status` for health checks)

## Quick start

```bash
cp .env.example .env
```

Also bring up the AI gateway -- a standalone deployment now, not part of this one -- unless you already have an external instance to point `AI_GATEWAY_HOST` (`.env`) at.

```bash
make up
make status
```

`.env.example` ships a working default for `QDRANT_COLLECTION=content`, but `AI_GATEWAY_EMBEDDINGS_MODEL`/`AI_GATEWAY_REASONING_MODEL`/`AI_GATEWAY_RERANKER_MODEL` have no default and must each be set in `.env` to a model alias your AI gateway is actually configured to serve. The reasoning alias must be set and resolvable before `mcp-server` will start — `answer_question`'s generation step always runs. `search_documents` and retrieval-only use don't need a reasoning model. See [Configuration](#configuration) for every combination.

`make status` polls `mcp-server`/Qdrant (plus `open-webui`/`pipelines` if the `open-webui` profile is up) and reports whether they're actually responding, not just "container running" -- it doesn't check the AI gateway, since that lives outside this compose project (check it via its own project's `docker compose ps`).

`make up` alone is enough to use `mcp-server` as a remote MCP connector from Claude or another MCP client -- Open WebUI is a separate, optional chat frontend on top of it. See [Open WebUI (optional)](#open-webui-optional) below if you want it.

To inspect what's actually in the vector DB (collections, points, payloads) after running `make ingest`, open **http://localhost:6333/dashboard** — Qdrant's built-in browsing UI, no extra service required.

## Open WebUI (optional)

Not required to use `mcp-server` -- it's a separate chat frontend on top of it, gated behind the `open-webui` Compose profile so it doesn't come up with plain `make up`.

```bash
make webui-up
```

| Service    | URL                        | Purpose                                            |
|------------|-----------------------------|-----------------------------------------------------|
| Open WebUI | http://localhost:4000      | Chat frontend                                       |
| pipelines  | http://localhost:9099      | Open WebUI Pipelines runtime (used by Open WebUI)   |

Once it's healthy (`make status`), open **http://localhost:4000**. On first visit you'll be asked to create a local admin account (this is Open WebUI's own auth, stored in its own volume — not an external account). The **RAG (UE Docs)** model (backed by `pipelines` → `mcp-server`'s `answer_question`, see [`pipelines`](#service-links) above) should already appear in the model picker.

`make webui-down` stops `open-webui`/`pipelines` without touching the rest of the stack or their data volumes; `make webui-logs` tails their logs. Both need `make up` (`qdrant`/`mcp-server`) already running, since `open-webui` depends on `pipelines`, which calls `mcp-server` over MCP.

## Configuration

Infra/credentials/deployment settings live in `.env` (copy it from `.env.example`); pipeline tuning knobs live in each service's own `config.yml` (see below) instead.

| Variable                                       | Default                  | Purpose                                                                                                                                                                                                            |
|------------------------------------------------|--------------------------|--------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| **AI gateway**                                 |                          |                                                                                                                                                                                                                    |
| `AI_GATEWAY_HOST`                              | **required, no default** | Host/IP other services connect to for the AI gateway (reasoning/embeddings/reranking), which runs as its own standalone project this repo doesn't manage                                                           |
| `AI_GATEWAY_PORT`                              | `4000`                   | Port `AI_GATEWAY_HOST` is reached on -- match whatever port your gateway publishes it as (4000 is own default)                                                                                                     |
| `AI_GATEWAY_AUTH_KEY`                           | **required, no default** | Sent by `ingest`/`mcp-server` on every AI gateway request (embeddings/reasoning/reranking), on the header/template below                                                                                             |
| `AI_GATEWAY_AUTH_HEADER`                       | **required, no default** | Header name `AI_GATEWAY_AUTH_KEY` is sent on                                                                                                                                                                          |
| `AI_GATEWAY_AUTH_VALUE_TEMPLATE`               | **required, no default** | Template for the header value above (`{key}` is replaced with `AI_GATEWAY_AUTH_KEY`)                                                                                                                                  |
| `AI_GATEWAY_EMBEDDINGS_MODEL`                  | **required, no default** | AI gateway model alias for embeddings; used by both `ingest` and `mcp-server` so query/document vectors share one space                                                                                            |
| `AI_GATEWAY_REASONING_MODEL`                   | **required, no default** | AI gateway model alias for reasoning; used by `mcp-server` for `answer_question`'s generation step, which always runs                                                                                                 |
| `AI_GATEWAY_RERANKER_MODEL`                    | **required, no default** | AI gateway model alias for reranking; only read when `mcp-server`'s `search_tools.reranker.enabled` is `true`                                                                                                         |
| **Qdrant**                                     |                          |                                                                                                                                                                                                                    |
| `QDRANT_COLLECTION`                            | **required, no default** | Qdrant collection used by both `ingest` and `mcp-server`                                                                                                                                                           |
| `QDRANT_HTTP_PORT`                             | `6333`                   | Qdrant REST API port on the host                                                                                                                                                                                   |
| `QDRANT_GRPC_PORT`                             | `6334`                   | Qdrant gRPC port on the host                                                                                                                                                                                       |
| **MCP Server**                                 |                          |                                                                                                                                                                                                                    |
| `MCP_SERVER_PORT`                              | `8000`                   | `mcp-server`'s streamable-HTTP port on the host                                                                                                                                                                    |
| **Open WebUI / pipelines (optional `open-webui` profile)** |              |                                                                                                                                                                                                                    |
| `OPEN_WEBUI_PORT`                              | `3000`                   | Open WebUI port on the host                                                                                                                                                                                        |
| `PIPELINES_PORT`                               | `9099`                   | `pipelines`'s HTTP port on the host                                                                                                                                                                                |
| `PIPELINES_API_KEY`                            | **required, no default** | Shared auth key `open-webui` and `pipelines` use to talk to each other over the OpenAI-compatible connection. Change it from the upstream default if `pipelines` is ever reachable from outside the Docker network |
| **Ingest**                                     |                          |                                                                                                                                                                                                                    |
| `FORCE_INGEST`                                 | `false`                  | Ingest run mode (see [Ingesting content](#ingesting-content)); a run-mode toggle, not a static setting, so it stays an env var                                                                                     |
| **Logging**                                    |                          |                                                                                                                                                                                                                    |
| `LOG_LEVEL`                                    | `INFO`                   | Log level for `ingest`/`mcp-server` (`services/_common/logging_config.py`); one of Python logging's level names (`DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`)                                                  |
| **Monitoring (optional `monitoring` profile)** |                          |                                                                                                                                                                                                                    |
| `TEMPO_PORT`                                   | `3200`                   | Tempo's HTTP port on the host (traces) — only relevant with the `monitoring` profile up, see [Observability / monitoring](#observability--monitoring-optional)                                                     |
| `LOKI_PORT`                                    | `3100`                   | Loki's HTTP port on the host (logs) — monitoring profile only                                                                                                                                                      |
| `PROMETHEUS_PORT`                              | `9090`                   | Prometheus's web UI port on the host — monitoring profile only                                                                                                                                                     |
| `CADVISOR_PORT`                                | `8085`                   | cAdvisor's web UI port on the host (per-container CPU/mem/network) — monitoring profile only                                                                                                                       |
| `NODE_EXPORTER_PORT`                           | `9101`                   | node-exporter's metrics port on the host (host-level CPU/mem/network/disk) — monitoring profile only                                                                                                               |
| `GRAFANA_PORT`                                 | `3001`                   | Grafana's web UI port on the host (dashboards over Tempo/Loki/Prometheus) — monitoring profile only                                                                                                                |

Required environment variables have no fallback in `docker-compose.yml` (`${VAR:?...}`) — `make up`/`make ingest` fail fast with a clear message if any of them is unset, rather than silently running against the wrong model, collection, or host, or failing generation later at request time. `.env.example` ships a working value for `QDRANT_COLLECTION` and a placeholder host for `AI_GATEWAY_HOST`; the three model aliases still need to be set explicitly to whatever your AI gateway actually names them (see the table above).

Which models actually run behind each alias, their sizing, and GPU support are entirely the AI gateway deployment's concern, not this repo's -- this project only ever sees the alias name and the gateway's response.

### `config.yml` files

Both `ingest` and `mcp-server` are bind-mounted (`./services/ingest:/app`-equivalent via their build context, `./services/mcp_server:/app`), so editing a `config.yml` and restarting/rerunning the service picks up the change — no rebuild needed for `mcp-server` (bind-mounted at runtime); `ingest` rebuilds automatically every run (`make ingest`/`make ingest-force` always pass `--build`), so its `config.yml` edits apply on the next run without a separate step.

- **[`services/mcp_server/config.yml`](./services/mcp_server/config.yml)** — one top-level key, `search_tools` (everything behind `search_documents`/`answer_question`): reranker on/off, `top_k_retrieve`/`top_k_rerank`, `reasoning` (its `model` comes from `AI_GATEWAY_REASONING_MODEL`, see the table above), and `generation` -- the prompt/sampler options for that reasoning model: a short prompt plus a JSON schema (`response_schema`) that constrains the model's output at the sampler level instead of relying on it following a textual format instruction. Every field has an explanatory comment in the file itself.
- **[`services/ingest/config.yml`](./services/ingest/config.yml)** — content/state paths, chunking (`max_chars`/`min_chars`), and `upsert_batch_size`.
- **AI gateway config** — which actual models each alias (`AI_GATEWAY_EMBEDDINGS_MODEL`/`AI_GATEWAY_REASONING_MODEL`/`AI_GATEWAY_RERANKER_MODEL`) routes to, and their own tuning (e.g. reranker's `max_length`), live in that gateway's own config, not in this repo.

## Make targets

| Target                 | Description                                                                                                                                                                                                                                                                                      |
|------------------------|--------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| `make up`              | Start the core stack in the background (`qdrant`/`mcp-server` only -- Open WebUI/`pipelines` are gated behind the `open-webui` profile, see `make webui-up`; the AI gateway is a separate, externally managed deployment)                                                                       |
| `make down`            | Stop everything (containers removed, volumes kept), including the `open-webui`/`monitoring` profiles if either was up -- compose only stops non-profiled services by default, so this passes both profiles explicitly                                                                          |
| `make restart`         | `down` followed by `up` -- brings back the core stack (`qdrant`/`mcp-server`) only; re-run `make webui-up`/`make monitoring-up` afterward if either profile was up                                                                                                                               |
| `make status`          | Show container status plus a health check against each service's HTTP port                                                                                                                                                                                                                       |
| `make ps`              | Alias for `make status`                                                                                                                                                                                                                                                                          |
| `make logs`            | Tail logs from all services                                                                                                                                                                                                                                                                      |
| `make mcp-logs`        | Tail logs from `mcp-server` only                                                                                                                                                                                                                                                                 |
| `make ingest`          | Run the ingest job once (incremental — only new/changed/removed files)                                                                                                                                                                                                                           |
| `make ingest-force`    | Run the ingest job with a full collection rebuild                                                                                                                                                                                                                                                |
| `make interactive_test` | Run `services/mcp_server/interactive_test.py` -- an interactive CLI walkthrough of the RAG pipeline, step by step, against the live stack (Qdrant + AI gateway)                                                                                                                                 |
| `make webui-up`        | Start Open WebUI + `pipelines` on top of the running core stack — see [Open WebUI (optional)](#open-webui-optional)                                                                                                                                                                              |
| `make webui-down`      | Stop Open WebUI + `pipelines` (volumes kept), without touching the core stack                                                                                                                                                                                                                    |
| `make webui-logs`      | Tail logs from Open WebUI + `pipelines`                                                                                                                                                                                                                                                          |
| `make monitoring-up`   | Start the optional observability stack (Tempo/Loki/Prometheus/Grafana/cAdvisor/node-exporter) on top of the running stack — see [Observability / monitoring](#observability--monitoring-optional)                                                                                                |
| `make monitoring-down` | Stop the observability stack's containers (volumes kept)                                                                                                                                                                                                                                         |
| `make monitoring-logs` | Tail logs from the observability stack's containers                                                                                                                                                                                                                                              |
| `make clean`           | **Destructive.** Stops the stack (including the `open-webui` profile and monitoring, if either was up) and deletes all volumes (Qdrant data, Open WebUI accounts/settings, ingest manifest, and monitoring's own trace/log/metric/dashboard data). Doesn't touch the AI gateway's own volumes -- see its own project for that |

## Ingesting content

`make ingest` walks `./content`, strips Hugo shortcodes/HTML, chunks each page by heading, embeds each chunk with `AI_GATEWAY_EMBEDDINGS_MODEL`, and upserts into the `QDRANT_COLLECTION` collection. Re-running it only touches files that changed since the last run (tracked in a manifest); `make ingest-force` rebuilds the collection from scratch. See [`services/ingest/`](./services/ingest) for details.

`make ingest` (no force) auto-escalates to a full rebuild if `QDRANT_COLLECTION` doesn't exist yet or exists but has zero points, regardless of what the manifest says -- the manifest is keyed by file path, not by collection name, so switching `QDRANT_COLLECTION` without wiping the manifest would otherwise leave the new collection empty.

`make ingest-force` drops and recreates the collection (`recreate_collection` -- delete + create) the moment the first non-empty file is embedded, then upserts as it goes; it does not wait until every file has been processed. If literally every file is empty/draft, the existing collection is left untouched (neither wiped nor recreated).

Change detection (incremental mode) is per-file, not per-chunk: each file's SHA-256 (raw bytes) is compared against the hash stored for it in the manifest (`STATE_DIR/manifest.json`). A new file, or one whose hash no longer matches, has all of its existing Qdrant points deleted (matched by the `source_path` payload field) and every one of its chunks re-embedded and re-upserted -- there's no partial/paragraph-level diffing within a file. A file removed from `./content` has its points deleted and its manifest entry dropped. Files that didn't change are skipped entirely -- no delete, no re-embed, no re-upsert.

Progress is logged per file as `[i/N, X%]`, and both the manifest and any already-embedded points are saved incrementally as the run goes (not buffered until the end) -- an interrupted run keeps whatever it completed up to that point instead of losing the whole run.

## Observability / monitoring (optional)

Off by default. `make up`/`docker compose up` never starts these containers -- they're gated behind Compose's `monitoring` profile in `docker-compose.yml`. None of the core services (`ingest`/`mcp-server`, or the external AI gateway) fail or behave differently when the monitoring stack is down; they emit OTLP traces on a best-effort basis and simply have nowhere to send them until `otel-collector` is up.

```bash
make monitoring-up
```

| Service        | URL                     | Purpose                                                                                                                                |
|----------------|-------------------------|----------------------------------------------------------------------------------------------------------------------------------------|
| Grafana        | http://localhost:3001   | Dashboards over the three stores below (anonymous viewer access enabled, no login needed)                                              |
| Prometheus     | http://localhost:9090   | Metrics store/query UI (container stats from cAdvisor, host stats from node-exporter, plus Tempo's span-metrics)                       |
| Tempo          | http://localhost:3200   | Trace store (queried through Grafana, not usually opened directly)                                                                     |
| Loki           | http://localhost:3100   | Log store — retrieved-chunk/answer payloads correlated by `trace_id` (queried through Grafana)                                         |
| cAdvisor       | —                       | Per-container CPU/mem/network exporter, scraped by Prometheus (no useful standalone UI here)                                           |
| node-exporter  | —                       | Host-level CPU/mem/network/disk exporter, scraped by Prometheus                                                                        |
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
- **`qdrant-model-migration`** — safely changing `AI_GATEWAY_EMBEDDINGS_MODEL` without leaving stale/incompatible vectors in the `content` collection.
- **`qdrant-monitoring`** — metrics, health checks, and debugging beyond the basic TCP check in `make status`.

If a plugin install doesn't apply automatically in an existing session, open `/plugin` once (or restart Claude Code) to pick up the project settings.

## Connecting the MCP server

`mcp-server` speaks MCP over streamable HTTP at `http://<host>:${MCP_SERVER_PORT}/mcp` (default `http://localhost:8000/mcp`) and exposes two tools:

- `search_documents(query, top_k)` — returns the raw reranked source chunks (title, path, heading, text, scores).
- `answer_question(query, top_k)` — runs the full pipeline and returns a generated answer with cited sources.

Queries can be in Russian or English; `answer_question` replies in whatever language the query was asked in. See [`services/mcp_server/`](./services/mcp_server) for the retrieval/rerank/generation pipeline details.

- **Claude (Console / Desktop / claude.ai):** add it as a remote MCP connector using the URL above (`http://localhost:8000/mcp` if running locally, or a reachable host/port otherwise).
- **Open WebUI:** add it under Settings → Tools as an MCP tool server using the same URL (from inside the compose network, other containers should reach it at `http://mcp-server:8000/mcp` instead of `localhost`).

### Pipeline steps

`answer_question` runs all 17 steps below in order (`RagPipeline.STEP_ORDER`, `services/mcp_server/src/libs/pipeline.py`), threading one `PipelineState` object forward. `search_documents` runs the same pipeline but with `want_answer=False`, so the last three steps (context/generate/parse) become no-ops and it returns after `merge_results`/`summary_search`. Config knobs referenced below live in `services/mcp_server/config.yml`, under `search_tools`.

#### 1. `rewrite_query`

Rephrases the raw user query into a short, formal, documentation-style search query via an LLM call (`search_tools.query_rewrite`, off by default). Needed because embeddings retrieve better against text that looks like the documentation's own wording than against a casual question. Expected output: `search_query`, a rewritten query in the same language — or, when disabled, `search_query` is just a passthrough copy of `query`.

#### 2. `embed_query`

Embeds `search_query` into a dense vector via the AI gateway's embeddings model. Needed as the input to both the dense half of `hybrid_search` and `route_query`'s classification. Expected output: `vector`, a fixed-dimension float list.

#### 3. `route_query`

Classifies the query as `point` (a specific value/parameter/step) or `global` (a broad overview) by cosine similarity against a small set of hardcoded reference examples (`search_tools.router`). Needed because a `global` query should also pull from the page-level summary index, which a `point` query gains nothing from. Expected output: `route_type`, one of `"point"`/`"global"` — feeds `summary_search` later, doesn't otherwise change retrieval.

#### 4. `correct_typos`

Fuzzy-corrects each token of `search_query` against `vocab.json` (titles/headings/tags indexed by `ingest`), using rapidfuzz (`search_tools.typo_correction`, on by default, local, no network call). Needed because the sparse/lexical branch below matches on exact tokens — a typo would otherwise silently drop that token's contribution. Expected output: `lexical_query`, token-corrected text (unchanged if no vocab is loaded yet, or no token clears the similarity threshold).

#### 5. `expand_synonyms`

Appends known synonyms (`services/mcp_server/synonyms.yml`) of matched terms to `lexical_query` (`search_tools.synonym_expansion`, on by default, local). Needed so the exact-token sparse branch still matches when the user's wording differs from the documentation's own terminology. Expected output: `lexical_query`, with synonym terms appended.

#### 6. `compute_sparse_vector`

Builds a sparse (term-frequency) vector from `lexical_query`. Needed as the lexical half of the hybrid search below, catching exact-token matches (identifiers, flags, config keys) that a dense embedding alone can blur together with semantically-similar-but-wrong text. Expected output: `sparse_vec`.

#### 7. `build_filters`

Builds a Qdrant `Filter` from the caller-supplied `title`/`description`/`source_path`/`tags` arguments, if any were passed to the tool call. Needed to scope retrieval to a specific page/section when the caller already knows where to look. Expected output: `query_filter` — `None` when no filter arguments were given, so retrieval is unrestricted.

#### 8. `exact_match_search`

Looks up an exact match against the `identifiers` payload field (config keys, CLI flags, dotted method paths — anything `IDENTIFIER_RE` extracted at ingest time), but only when `search_query` itself looks identifier-shaped. Needed because hybrid search can rank a semantically-close chunk above the one literal chunk that actually defines the identifier being asked about. Expected output: `exact_hits`, a list of exact-match points (usually empty — most queries aren't a bare identifier).

#### 9. `hybrid_search`

Runs the dense (`vector`) and sparse (`sparse_vec`) queries against Qdrant in parallel, fused by RRF (Reciprocal Rank Fusion), scoped by `query_filter`. Needed as the main retrieval step — combining both retrieval modes catches both "semantically similar" and "exact term" matches in one ranked list. Expected output: `hits`, up to `search_tools.retrieval.top_k_retrieve` candidates.

#### 10. `dedup_near_duplicates`

Drops a hit whose dense-vector cosine similarity to a higher-ranked hit already kept is >= `search_tools.retrieval.dedup_similarity_threshold`, walking `hits` in their post-`hybrid_search` rank order so the higher-ranked hit of a near-duplicate pair always survives. Needed because near-identical passages (the same explanation repeated across two pages) otherwise burn multiple context slots on one fact instead of giving the model more distinct coverage. Expected output: `hits`, trimmed in place (unchanged when the threshold is `null`).

#### 11. `cap_chunks_per_source`

Caps how many of `hits` may come from the same `source_path` (`search_tools.retrieval.max_chunks_per_source`). Needed so one large page can't crowd out every other relevant source in the candidate pool before reranking even runs. Expected output: `hits`, trimmed in place (unchanged when the cap is `null`).

#### 12. `rerank_results`

Scores each hit's chunk text against `search_query` with a cross-encoder reranker service (`search_tools.reranker`, off by default — falls back to plain Qdrant order when disabled or when the reranker call itself fails). Needed because dense/sparse similarity alone is a weaker relevance signal than a cross-encoder that actually reads query and chunk together. Expected output: `reranked`, a list of `(hit, score)` pairs, sorted, sliced to `top_k_rerank`, optionally cut further by `confidence_cutoff`.

#### 13. `merge_results`

Merges `exact_hits` (first, deduplicated) with `reranked` into one flat list of plain dicts (title/description/source_path/heading/updated/text/scores). Needed to give every downstream step (context building, the `search_documents` response) one uniform shape regardless of which retrieval path a chunk came from. Expected output: `results`.

#### 14. `summary_search`

For `global`-routed queries only, queries the page-level summary index (`<QDRANT_COLLECTION>_summaries`, one point per source page, written by `ingest`) by dense similarity. Needed because a broad "how does X work overall" question is better served by whole-page summaries than by individual chunks. Expected output: `summary_hits` — always empty for `point`-routed queries.

#### 15. `build_context`

(`answer_question` only — no-op when `want_answer` is `False`.) Renders `results` + `summary_hits` into one context block using `search_tools.source_template`/`summary.source_template`, numbering each item `id=1..N`. Needed as the exact text handed to the generation model — the `id` numbering here is what the model's `sources` output later refers back to. Expected output: `context_items` (the raw list) and `context_text` (the rendered block).

#### 16. `generate_answer`

(`answer_question` only — no-op when there's no context.) Sends `context_text` + the original `query` to the reasoning model, constrained by `search_tools.generation.response_schema` to a `{reasoning, answer, sources}` JSON shape. Needed to produce the actual answer, grounded only in the retrieved context (the system prompt forbids outside knowledge). Expected output: `generation_text`, the model's raw JSON reply as a string.

#### 17. `parse_answer`

(`answer_question` only.) Parses `generation_text` as JSON (`parse_structured_answer`, `services/mcp_server/src/libs/config.py`) and maps the model's bare integer `sources` ids back to `[id] title (path)` citation strings using `context_items`. Needed to turn the model's raw JSON reply into the three fields the tool actually returns. Expected output: `answer`, `reasoning`, `sources` — or, when `context_items` was empty, a fixed "no relevant documents" `answer` with `reasoning=None`/`sources=[]` (the generation/parse steps above never ran in that case).

### Startup warmup

`mcp-server` pays every model's cold-start cost once at startup instead of on whichever request happens to arrive first (`services/mcp_server/src/libs/wiring.py`'s `_warm_up()`, run at the end of `build_pipeline()`):

- `query_router` — embeds the 12 hardcoded reference examples `QueryRouter` classifies queries against (`search_tools.router.point_examples`/`global_examples` in `config.yml`); also warms the embedding model itself, since it's the same AI gateway call `embed_query` uses.
- `reasoning_model` — one throwaway `generate()` call ("Reply with OK." / "OK"), warming the model behind `AI_GATEWAY_REASONING_MODEL` (used by `query_rewrite` and `answer_question`'s generation step).
- `reranker_model` — one throwaway `rerank()` call, only when `search_tools.reranker.enabled` is `true`.

Each step logs its own duration, bracketed by an overall start/finish line:

```
warmup started
warmup step=query_router duration_ms=2985.6
warmup step=reasoning_model duration_ms=821.9
warmup finished total_duration_ms=3808.5
```

(`docker compose logs mcp-server`, or `make mcp-logs`.) What actually gets reused afterward differs by layer: `QueryRouter`'s embedded vectors are cached in the process's own memory for as long as `mcp-server` keeps running; the AI gateway backend (e.g. Ollama) keeps a warmed model resident in its own memory for some `keep_alive` window after last use, independent of this process — a long enough gap between requests still pays the cold-load cost again, warmup or not; and the `httpx.Client` each AI gateway client holds (`services/_common/clients/ai_gateway_client.py`) pools its TCP connection, so the warmup request's connection is what the first real request reuses too.

## Stopping the stack

| Goal                                                    | Command                                                                       | Effect                                                                                                                                                                                                                                                                                                                           |
|---------------------------------------------------------|-------------------------------------------------------------------------------|----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| Stop everything, keep data                              | `make down`                                                                   | Stops/removes containers, including the `open-webui`/`monitoring` profiles if either was up; Qdrant data, Open WebUI accounts, and ingest manifest all survive in their volumes for the next `make up`/`make webui-up`. Doesn't touch the AI gateway -- it's a separate project, stop it from its own folder                     |
| Stop everything and start fresh                         | `make clean`                                                                  | **Destructive.** Same as `make down` but also deletes every volume in this project (Qdrant data, Open WebUI accounts/settings, ingest manifest, and monitoring's trace/log/metric/dashboard data if that profile was ever started). Doesn't touch the AI gateway's own volumes                                                   |
| Restart everything                                      | `make restart`                                                                | `make down` then `make up` — brings back the core stack only (`qdrant`/`mcp-server`); re-run `make webui-up`/`make monitoring-up` afterward if either profile was up before                                                                                                                                                      |
| Stop just Open WebUI                                    | `make webui-down`                                                             | Stops `open-webui`/`pipelines` only; core services (`qdrant`/`mcp-server`) keep running                                                                                                                                                                                                                                          |
| Stop just the monitoring stack                          | `make monitoring-down`                                                        | Stops Tempo/Loki/Prometheus/Grafana/cAdvisor/node-exporter only; core services (`qdrant`/`mcp-server`) and `open-webui`/`pipelines` (if up) keep running                                                                                                                                                                         |
| Stop one service                                        | `docker compose stop <service>` (e.g. `docker compose stop open-webui`)       | Stops just that container; others keep running. `mcp-server`/`ingest` depend on `qdrant` being up, so stopping it breaks retrieval for the rest. The AI gateway lives outside this compose project entirely (per `AI_GATEWAY_HOST`) -- stopping it from its own project just makes calls that need it fail at request time       |
| Restart one core service after editing its `config.yml` | `docker compose restart <service>` (e.g. `docker compose restart mcp-server`) | `mcp-server`/`ingest` are bind-mounted (see [`config.yml` files](#configyml-files)), so this is enough to pick up an edit — no rebuild needed for `mcp-server`; `ingest` is one-shot and always rebuilds on its own `make ingest`/`make ingest-force` invocation. Same applies to the AI gateway's own deployment for its config |

`ingest` never needs "stopping" in the above sense — it's a one-shot job (`profiles: ["ingest"]`, `restart: "no"`) that exits on its own once a run finishes; it isn't part of the always-on stack `make up`/`make down` manage.

## Troubleshooting

- **A service in `make status` shows "NOT RESPONDING" right after `make up`.** Most services take a few seconds to a few minutes to become healthy on first start. Check `docker compose ps` for its actual state and `docker compose logs -f <service>` for what it's doing; re-run `make status` after it settles. See the specific cases below for the slow-first-start services.
- **Model pulling/loading behind a AI gateway alias is slow or seems stuck.** This is now that backing deployment's concern, not this repo's or the gateway's -- see its own troubleshooting docs.
- **First `search_documents`/`answer_question` call is slow.** `mcp-server` warms the embedding/reasoning/reranker models at startup (see [Startup warmup](#startup-warmup)), so this shouldn't be the cause anymore -- check `make mcp-logs` for the `warmup ...` lines and their timings first. If warmup itself was slow, or ran too long ago (the AI gateway backend can evict an idle model after its own `keep_alive` window), the next call pays the cold-load cost again regardless. Whatever cross-encoder model backs `AI_GATEWAY_RERANKER_MODEL` may also download on first use, not at build time -- see that reranker deployment's own troubleshooting docs.
- **"RAG (UE Docs)" model is missing from Open WebUI's picker.** Two possible causes: (1) `pipelines` installs `rag_pipeline.py`'s `requirements:` frontmatter packages into its own venv on every container start (not baked into the image) — check `docker compose logs pipelines` for install progress or errors; (2) a brand-new Open WebUI account needs to complete first-visit admin signup before any model (Ollama or pipelines) appears in the picker at all.
- **Open WebUI's `/workspace/models` page is empty even though models are pulled.** Expected — that page only lists model *entries* you've explicitly pinned there. The actual pulled Ollama models (and the pipelines model) show up in the chat page's model dropdown instead.
- **Chatting against the embedding model in Open WebUI fails with an HTTP 400.** `AI_GATEWAY_EMBEDDINGS_MODEL` is embedding-only and doesn't support Ollama's `/api/chat` endpoint. Always chat against `AI_GATEWAY_REASONING_MODEL`; reserve `AI_GATEWAY_EMBEDDINGS_MODEL` for embedding calls.
- **A port is already in use on the host.** Port defaults in `.env.example` are dev-convenience values, not guarantees — if one collides with something else already running on your machine, change the corresponding `*_PORT` variable in `.env` (not the compose file) and `make restart`.
- **`make up`/`make ingest` fails immediately with a message about `AI_GATEWAY_HOST`, `AI_GATEWAY_AUTH_KEY`, `AI_GATEWAY_EMBEDDINGS_MODEL`, `AI_GATEWAY_REASONING_MODEL`, `AI_GATEWAY_RERANKER_MODEL`, or `QDRANT_COLLECTION` being unset.** These have no fallback on purpose (`${VAR:?...}` in `docker-compose.yml`) — copy `.env.example` to `.env` if you haven't, or check you haven't accidentally deleted/commented out one of those lines.
- **`mcp-server` fails at startup complaining about a missing model.** `AI_GATEWAY_REASONING_MODEL` is required — `answer_question`'s generation step always runs, see [Configuration](#configuration). `mcp-server` fails fast rather than falling back silently, so the log line at `make mcp-logs` names exactly what's missing.
- **Switched `AI_GATEWAY_EMBEDDINGS_MODEL` and search results look wrong/empty.** Different embedding models produce vectors of different (and non-comparable) dimensions. A model swap always needs a full `make ingest-force`, not just `make ingest` — see [Embedding model](#embedding-model).
- **Traces/logs aren't showing up in Grafana.** The monitoring stack must be started separately with `make monitoring-up` — it's not part of `make up`. If it's already up, confirm `otel-collector` is healthy (`docker compose ps`) and that the service you're checking actually made a request recently (spans/logs are only emitted per-request, not continuously).
- **GPU isn't being used by Ollama.** GPU support is entirely that deployment's concern, not this repo's. Not supported on macOS at all (no GPU passthrough into Docker containers there). The reranker always runs on CPU regardless.
