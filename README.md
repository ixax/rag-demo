# RAG Stack

A local, Docker Compose-based foundation for a Retrieval-Augmented Generation (RAG) setup over the documentation in `./content`.

This repo currently provides the **infrastructure skeleton only**:

- **[Qdrant](https://qdrant.tech/)** — vector database.
- **[Ollama](https://ollama.com/)** — runs two local models on CPU:
  - an instruct model, intended for cleaning/normalizing content before it's embedded,
  - an embedding model, intended for turning content into vectors.
- **[Open WebUI](https://openwebui.com/)** — a simple, ChatGPT-like chat frontend wired to Ollama out of the box.

Turning `./content` into vectors in Qdrant (chunking, cleaning, embedding, upserting) and any query-time retrieval/agent/MCP wiring are **separate, not-yet-implemented work** on top of this base.

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

## Configuration

All configuration lives in `.env` (copy it from `.env.example`).

| Variable                | Default              | Purpose                                             |
|--------------------------|----------------------|------------------------------------------------------|
| `OLLAMA_INSTRUCT_MODEL`  | `llama3.2:3b`        | Instruct model pulled into Ollama                    |
| `OLLAMA_EMBED_MODEL`     | `nomic-embed-text`   | Embedding model pulled into Ollama                   |
| `QDRANT_HTTP_PORT`       | `6333`               | Qdrant REST API port on the host                     |
| `QDRANT_GRPC_PORT`       | `6334`               | Qdrant gRPC port on the host                         |
| `OLLAMA_PORT`            | `11434`              | Ollama API port on the host                          |
| `OPEN_WEBUI_PORT`        | `3000`               | Open WebUI port on the host                          |

Both models are CPU-sized on purpose (3B instruct model, small embedding model) so the stack runs without a GPU. Swap them for larger models in `.env` if you have the hardware, then run `make pull-models`.

## Make targets

| Target          | Description                                                                 |
|-----------------|-------------------------------------------------------------------------------|
| `make up`       | Start the stack in the background                                            |
| `make down`     | Stop the stack (containers removed, volumes kept)                            |
| `make restart`  | `down` followed by `up`                                                      |
| `make status`   | Show container status plus a health check against each service's HTTP port   |
| `make ps`       | Alias for `make status`                                                      |
| `make logs`     | Tail logs from all services                                                  |
| `make pull-models` | Re-run model pulling manually (useful after changing `.env` model names)  |
| `make clean`    | **Destructive.** Stops the stack and deletes all volumes (Qdrant data, pulled models, Open WebUI accounts/settings) |

## Verifying the stack manually

```bash
# Containers healthy?
docker compose ps

# Models pulled into Ollama?
docker compose exec ollama ollama list

# Qdrant reachable (empty collection list is expected — no data has been ingested yet)?
curl http://localhost:6333/collections
```

## What's not here yet

- Ingesting `./content` into Qdrant (chunking, cleaning with the instruct model, embedding, upserting).
- Any query-time retrieval, agent, or MCP integration for actually answering questions over the ingested content.

Both are planned as separate follow-up work.
