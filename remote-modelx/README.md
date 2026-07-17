# remote-modelx

A standalone Docker Compose deployment of [Ollama](https://ollama.com/) and a cross-encoder reranking HTTP service, meant to run together and be copied into another project wholesale. Not on the same Docker network as any consumer project -- consumer services reach these over the host (`OLLAMA_HOST`/`OLLAMA_PORT`, `RERANKER_HOST`/`RERANKER_PORT` in the consumer's own `.env`), not via container-name DNS.

## Quick start

```bash
cp .env.example .env   # set MODEL_EMBED and MODEL_RERANKER (ships a working default); MODEL_INSTRUCT_INTERNAL if you need local generation
make up
docker compose logs -f ollama-pull   # watch the Ollama model pull -- can take minutes
docker compose logs -f reranker      # watch it download MODEL_RERANKER on first request
```

On a host with an NVIDIA GPU reachable from Docker (Windows + Docker Desktop/WSL2, or Linux with the NVIDIA Container Toolkit; not macOS -- no GPU passthrough into Docker containers there):

```bash
make up-gpu
```

## What's here

- **`docker-compose.yml`** -- `ollama` (server, port `${OLLAMA_PORT:-11434}`), `ollama-pull` (one-shot job that pulls + warms `MODEL_EMBED`/`MODEL_INSTRUCT_INTERNAL`), and `reranker` (cross-encoder HTTP service, port `${RERANKER_PORT:-50051}`).
- **`docker-compose.gpu.yml`** -- optional override reserving the host's NVIDIA GPU for `ollama`.
- **`.env.example`** -- `MODEL_EMBED`/`MODEL_RERANKER` (required), everything else optional.
- **`Makefile`** -- `make up`/`up-gpu`/`down`/`clean`/`restart`/`status`/`logs`/`pull` (see targets below).
- **`ollama/`** -- `entrypoint.sh` (the `ollama-pull` script, bind-mounted).
- **`reranker/`** -- `Dockerfile` (CPU-only torch build), `src/` (FastAPI app + `libs/`, including its own config loading/logging/tracing), `config.yml` (`max_length`, bind-mounted -- edit and restart, no rebuild needed).

## Connecting a consumer project

Point the consumer project at this instance via its own `.env`:

```
OLLAMA_HOST=host.docker.internal     # or this host's IP/hostname if not on the same machine
OLLAMA_PORT=11434                    # match OLLAMA_PORT above if you changed it
RERANKER_HOST=host.docker.internal
RERANKER_PORT=50051                  # match RERANKER_PORT above if you changed it
```

`host.docker.internal` resolves from inside a container back to the Docker host on Docker Desktop (Mac/Windows) out of the box; on Linux, the consumer's own compose service needs an `extra_hosts: ["host.docker.internal:host-gateway"]` entry (Docker Engine 20.10+) for that name to resolve. If this stack runs on a different machine entirely, use that host's actual IP/hostname instead.

Model names (`MODEL_EMBED`, `MODEL_INSTRUCT_INTERNAL`, `MODEL_RERANKER`) must match between this folder's `.env` (what gets pulled/served) and the consumer project's `.env` (what it asks for) -- there's no other link between the two.

## Re-pulling / changing models

```bash
make pull                             # after changing MODEL_EMBED/MODEL_INSTRUCT_INTERNAL
docker compose up -d --build reranker # after changing MODEL_RERANKER
```

## Stopping / data

```bash
make down    # stop, keep pulled models and cached reranker weights
make clean   # stop and delete both
```

## Troubleshooting

- **Ollama model pulling is slow or seems stuck.** `docker compose logs -f ollama-pull`; exit code 0 means success. Re-run manually with `docker compose run --rm ollama-pull` if it's still missing afterwards.
- **First `/rerank` call is slow.** `MODEL_RERANKER` downloads from Hugging Face on first use, not at build time -- needs internet access from inside the container. Watch progress with `docker compose logs -f reranker`; subsequent calls reuse the cached weights in the `reranker_cache` volume.
- **A consumer project can't reach Ollama or the reranker.** Confirm `OLLAMA_HOST`/`OLLAMA_PORT`/`RERANKER_HOST`/`RERANKER_PORT` in the consumer's `.env`, that this stack is actually up (`docker compose ps`), and (on Linux) that the consumer's service has the `extra_hosts` entry above if it uses `host.docker.internal`.
- **GPU isn't being used.** Confirm you started with both compose files (`-f docker-compose.yml -f docker-compose.gpu.yml`), not just the base one.
- **CPU usage seems capped / rerank calls are slower than expected.** `RERANKER_NUM_THREADS` must match `docker-compose.yml`'s `cpus:` limit on the `reranker` service -- see `reranker/src/libs/model.py`'s `load_model()` docstring for why an unset/mismatched thread count fights that limit instead of respecting it.
