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

Ports above are the `.env.example` defaults; if you've overridden `*_PORT` in `.env`, substitute accordingly.

- **[Qdrant](https://qdrant.tech/)** — vector database. Ships with a built-in web dashboard for browsing collections/points, served at `/dashboard` on its own REST port — no separate viewer service needed.
- **[Ollama](https://ollama.com/)** — runs two local models on CPU:
  - an instruct model, used for answer generation (and available for content cleaning),
  - an embedding model, used both at ingest time and at query time.
- **[Open WebUI](https://openwebui.com/)** — a simple, ChatGPT-like chat frontend wired to Ollama out of the box.
- **`ingest`** — one-shot job that walks `./content`, chunks/cleans the Hugo markdown, embeds it, and upserts it into Qdrant. See [`ingest/`](./ingest).
- **`mcp-server`** — an MCP server exposing retrieval-augmented search/answering (embed query → Qdrant search → cross-encoder rerank → Ollama generation) over streamable HTTP, for use as a remote MCP connector from Open WebUI and Claude. See [`mcp_server/`](./mcp_server).

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

All configuration lives in `.env` (copy it from `.env.example`).

| Variable                | Default                     | Purpose                                                        |
|--------------------------|------------------------------|------------------------------------------------------------------|
| `OLLAMA_INSTRUCT_MODEL`  | `llama3.2:3b`                | Instruct model pulled into Ollama; used by `mcp-server` for answer generation |
| `OLLAMA_EMBED_MODEL`     | `bge-m3`                     | Embedding model pulled into Ollama; used by both `ingest` and `mcp-server` so query/document vectors share one space |
| `QDRANT_HTTP_PORT`       | `6333`                       | Qdrant REST API port on the host                                |
| `QDRANT_GRPC_PORT`       | `6334`                       | Qdrant gRPC port on the host                                    |
| `OLLAMA_PORT`            | `11434`                      | Ollama API port on the host                                     |
| `OPEN_WEBUI_PORT`        | `3000`                       | Open WebUI port on the host                                     |
| `MCP_SERVER_PORT`        | `8000`                       | `mcp-server`'s streamable-HTTP port on the host                 |
| `QDRANT_COLLECTION`      | `content`                    | Qdrant collection used by both `ingest` and `mcp-server`         |
| `RERANKER_MODEL`         | `BAAI/bge-reranker-v2-m3`    | Cross-encoder reranker `mcp-server` loads (multilingual, handles Russian) |
| `TOP_K_RETRIEVE`         | `20`                         | Candidates pulled from Qdrant before reranking                  |
| `TOP_K_RERANK`           | `5`                          | Top reranked chunks kept as generation context                  |

Both Ollama models are CPU-sized on purpose (3B instruct model, small embedding model) so the stack runs without a GPU. Swap them for larger models in `.env` if you have the hardware, then run `make pull-models`.

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

`make ingest` walks `./content`, strips Hugo shortcodes/HTML, chunks each page by heading, embeds each chunk with `OLLAMA_EMBED_MODEL`, and upserts into the `QDRANT_COLLECTION` collection. Re-running it only touches files that changed since the last run (tracked in a manifest); `make ingest-force` rebuilds the collection from scratch. See [`ingest/`](./ingest) for details.

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
- **`qdrant-model-migration`** — safely changing `OLLAMA_EMBED_MODEL` without leaving stale/incompatible vectors in the `content` collection.
- **`qdrant-monitoring`** — metrics, health checks, and debugging beyond the basic TCP check in `make status`.

If a plugin install doesn't apply automatically in an existing session, open `/plugin` once (or restart Claude Code) to pick up the project settings.

## Connecting the MCP server

`mcp-server` speaks MCP over streamable HTTP at `http://<host>:${MCP_SERVER_PORT}/mcp` (default `http://localhost:8000/mcp`) and exposes two tools:

- `search_documents(query, top_k)` — returns the raw reranked source chunks (title, path, heading, text, scores).
- `answer_question(query, top_k)` — runs the full pipeline and returns a generated answer with cited sources.

Queries can be in Russian or English; `answer_question` replies in whatever language the query was asked in. See [`mcp_server/`](./mcp_server) for the retrieval/rerank/generation pipeline details.

- **Claude (Console / Desktop / claude.ai):** add it as a remote MCP connector using the URL above (`http://localhost:8000/mcp` if running locally, or a reachable host/port otherwise).
- **Open WebUI:** add it under Settings → Tools as an MCP tool server using the same URL (from inside the compose network, other containers should reach it at `http://mcp-server:8000/mcp` instead of `localhost`).
