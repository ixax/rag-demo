# RAG Stack

A local, Docker Compose-based foundation for a Retrieval-Augmented Generation (RAG) setup over the documentation in `./content`.

## Service links

Once `make up` has finished and `make status` reports everything healthy:

| Service | URL | Purpose |
|---------|-----|---------|
| Open WebUI | http://localhost:3000 | Chat frontend |
| Qdrant dashboard | http://localhost:6333/dashboard | Browse collections, points, and payloads in the vector DB |
| Qdrant REST API | http://localhost:6333 | Raw API (used by `ingest`/`mcp-server`) |
| mcp-server | http://localhost:8000/mcp | MCP endpoint (streamable HTTP) |
| Ollama API | http://localhost:11434 | Raw API (used by Open WebUI/`mcp-server`) |
| pipelines | http://localhost:9099 | Open WebUI Pipelines runtime (used by Open WebUI) |

Ports above are the `.env.example` defaults; if you've overridden `*_PORT` in `.env`, substitute accordingly.

- **[Qdrant](https://qdrant.tech/)** — vector database. Ships with a built-in web dashboard for browsing collections/points, served at `/dashboard` on its own REST port — no separate viewer service needed.
- **[Ollama](https://ollama.com/)** — runs two local models on CPU:
  - an instruct model, used for answer generation (and available for content cleaning),
  - an embedding model, used both at ingest time and at query time.
- **[Open WebUI](https://openwebui.com/)** — a simple, ChatGPT-like chat frontend wired to Ollama out of the box, plus the `pipelines` connection below.
- **`pipelines`** — [Open WebUI Pipelines](https://github.com/open-webui/pipelines) runtime, registered in Open WebUI as an OpenAI-compatible connection. Loads [`services/open_webui_pipelines/rag_pipeline.py`](./services/open_webui_pipelines/rag_pipeline.py), which shows up as its own selectable model, **RAG (UE Docs)**, in Open WebUI's model picker. Selecting it and asking a question calls `mcp-server`'s `answer_question` tool directly over MCP and returns its answer — no chat model or tool-calling involved.
- **`ingest`** — one-shot job that walks `./content`, chunks/cleans the Hugo markdown, embeds it, and upserts it into Qdrant. See [`services/ingest/`](./services/ingest).
- **`mcp-server`** — an MCP server exposing retrieval-augmented search/answering (embed query → Qdrant search → cross-encoder rerank → Ollama generation) over streamable HTTP, for use as a remote MCP connector from Open WebUI and Claude. See [`services/mcp_server/`](./services/mcp_server).

## Prerequisites

- Docker
- Docker Compose v2 (the `docker compose` CLI plugin)
- `make`
- `curl` (used by `make status` for health checks)

## Quick start

```bash
cp .env.example .env
make up
make status
```

`make status` polls all three services and reports whether they're actually responding, not just "container running." The `ollama-pull` container pulls both configured models automatically on first `make up` — this can take a few minutes depending on model size and your connection. Check progress with:

```bash
docker compose logs -f ollama-pull
```

Once everything is healthy, open **http://localhost:3000** to reach Open WebUI. On first visit you'll be asked to create a local admin account (this is Open WebUI's own auth, stored in its own volume — not an external account). The Ollama models configured below should already appear in the model picker.

To inspect what's actually in the vector DB (collections, points, payloads) after running `make ingest`, open **http://localhost:6333/dashboard** — Qdrant's built-in browsing UI, no extra service required.

## Configuration

Infra/credentials/deployment settings live in `.env` (copy it from `.env.example`); pipeline tuning knobs live in each service's own `config.yml` (see below) instead.

| Variable                  | Default                   | Purpose                                                                                                                                       |
|---------------------------|---------------------------|------------------------------------------------------------------------------------------------------------------------------------------------|
| `MODEL_INSTRUCT_INTERNAL` | **required, no default**  | Instruct model pulled into Ollama; used by `mcp-server` for answer generation when `backend.type` is `ollama` (or unset)                       |
| `MODEL_EMBED`             | **required, no default**  | Embedding model pulled into Ollama; used by both `ingest` and `mcp-server` so query/document vectors share one space                           |
| `MODEL_RERANKER`          | `BAAI/bge-reranker-v2-m3` | Cross-encoder model for the `reranker` service, pulled from Hugging Face on first use                                                          |
| `MODEL_INSTRUCT_EXTERNAL` | (unset)                   | Instruct model for `mcp-server` answer generation when `backend.type` is `anthropic`; not an Ollama model, so `ollama-pull` doesn't touch it   |
| `QDRANT_COLLECTION`       | **required, no default**  | Qdrant collection used by both `ingest` and `mcp-server`                                                                                       |
| `QDRANT_HTTP_PORT`        | `6333`                    | Qdrant REST API port on the host                                                                                                                |
| `QDRANT_GRPC_PORT`        | `6334`                    | Qdrant gRPC port on the host                                                                                                                    |
| `OLLAMA_PORT`             | `11434`                   | Ollama API port on the host                                                                                                                     |
| `OPEN_WEBUI_PORT`         | `3000`                    | Open WebUI port on the host                                                                                                                     |
| `MCP_SERVER_PORT`         | `8000`                    | `mcp-server`'s streamable-HTTP port on the host                                                                                                 |
| `PIPELINES_PORT`          | `9099`                    | `pipelines`'s HTTP port on the host                                                                                                              |
| `FORCE_INGEST`            | `false`                   | Ingest run mode (see [Ingesting content](#ingesting-content)); a run-mode toggle, not a static setting, so it stays an env var                  |
| `ANTHROPIC_API_KEY`       | (unset)                   | Required only when `services/mcp_server/config.yml`'s `backend.type` is `anthropic`; a credential, so it stays out of the config file          |

`MODEL_INSTRUCT_INTERNAL`, `MODEL_EMBED`, and `QDRANT_COLLECTION` have no fallback in `docker-compose.yml` (`${VAR:?...}`) — `make up`/`make ingest` fail fast with a clear message if any is unset, rather than silently running against the wrong model/collection. `.env.example` ships working values for all three; copy it to `.env` and you're covered.

Both Ollama models are CPU-sized on purpose (3B instruct model, small embedding model) so the stack runs without a GPU. Swap them for larger models in `.env` if you have the hardware, then run `make pull-models`.

On a host with an NVIDIA GPU reachable from Docker (Windows + Docker Desktop/WSL2, or Linux with the NVIDIA Container Toolkit), run `make up-gpu` instead of `make up` to reserve it for `ollama` — see [`docker-compose.gpu.yml`](./docker-compose.gpu.yml) for requirements. Not applicable on macOS (no GPU passthrough into Docker containers there, Metal or otherwise). Only `ollama` is covered; `reranker` still runs on CPU (its Dockerfile installs the CPU-only torch wheel).

### `config.yml` files

Both `ingest` and `mcp-server` are bind-mounted (`./services/ingest:/app`-equivalent via their build context, `./services/mcp_server:/app`), so editing a `config.yml` and restarting/rerunning the service picks up the change — no rebuild needed for `mcp-server` (bind-mounted at runtime); `ingest` rebuilds automatically every run (`make ingest`/`make ingest-force` always pass `--build`), so its `config.yml` edits apply on the next run without a separate step.

- **[`services/mcp_server/config.yml`](./services/mcp_server/config.yml)** — reranker on/off, `top_k_retrieve`/`top_k_rerank`, the generation `backend` (`""` to skip generation entirely, `"ollama"`, or `"anthropic"` with `max_tokens`; its `model` comes from `MODEL_INSTRUCT_INTERNAL`/`MODEL_INSTRUCT_EXTERNAL`, see the table above), and the system prompt. Every field has an explanatory comment in the file itself.
- **[`services/ingest/config.yml`](./services/ingest/config.yml)** — content/state paths, chunking (`max_chars`/`min_chars`), and `upsert_batch_size`.
- **[`services/reranker/config.yml`](./services/reranker/config.yml)** — `max_length` (token truncation); the cross-encoder model itself is `MODEL_RERANKER` (see the table above).

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
| `make up`       | Start the stack in the background                                            |
| `make down`     | Stop the stack (containers removed, volumes kept)                            |
| `make restart`  | `down` followed by `up`                                                      |
| `make status`   | Show container status plus a health check against each service's HTTP port   |
| `make ps`       | Alias for `make status`                                                      |
| `make logs`     | Tail logs from all services                                                  |
| `make mcp-logs` | Tail logs from `mcp-server` only (useful while it downloads the reranker on first run) |
| `make pull-models` | Re-run model pulling manually (useful after changing `.env` model names)  |
| `make ingest`   | Run the ingest job once (incremental — only new/changed/removed files)       |
| `make ingest-force` | Run the ingest job with a full collection rebuild                       |
| `make clean`    | **Destructive.** Stops the stack and deletes all volumes (Qdrant data, pulled models, Open WebUI accounts/settings, ingest manifest, reranker cache) |

## Verifying the stack manually

```bash
# Containers healthy?
docker compose ps

# Models pulled into Ollama?
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

Queries can be in Russian or English; `answer_question` replies in whatever language the query was asked in. See [`services/mcp_server/`](./services/mcp_server) for the retrieval/rerank/generation pipeline details.

- **Claude (Console / Desktop / claude.ai):** add it as a remote MCP connector using the URL above (`http://localhost:8000/mcp` if running locally, or a reachable host/port otherwise).
- **Open WebUI:** add it under Settings → Tools as an MCP tool server using the same URL (from inside the compose network, other containers should reach it at `http://mcp-server:8000/mcp` instead of `localhost`).
