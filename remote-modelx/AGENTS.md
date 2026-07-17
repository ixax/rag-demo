# AGENTS.md -- remote-modelx/

Standalone deployment, decoupled from any consumer project's own compose network -- see `README.md` for the "why" and how consumer services connect (`OLLAMA_HOST`/`OLLAMA_PORT`, `RERANKER_HOST`/`RERANKER_PORT`).

## Ollama

- `ollama-pull`'s script lives in `ollama/entrypoint.sh`, bind-mounted rather than inlined as `command:` in `docker-compose.yml`.
- `OLLAMA_KEEP_ALIVE: "-1"` on the `ollama` service (never auto-unload a loaded model) is why `entrypoint.sh` runs a throwaway `ollama run "$MODEL" "hi"` after each `pull` -- warms the model into memory once at pull time instead of eating the cold-load stall on a consumer's first real request.
- `docker-compose.gpu.yml` only touches the `ollama` service's `deploy.resources.reservations` -- it's an override applied with `-f docker-compose.yml -f docker-compose.gpu.yml`, never a replacement for the base file.

## Reranker

- `reranker/src/server.py` is handlers + wiring only -- config schema, model loading, and scoring live in `reranker/src/libs/` (`config.py`, `model.py`); those functions take what they need as arguments rather than reading env/config themselves.
- `reranker/src/libs/logging_config.py`, `tracing.py`, and `yaml_config.py` are this service's own config-loading/logging/tracing code, not shared with anything else in `remote-modelx/`.
- `RERANKER_NUM_THREADS` must match `docker-compose.yml`'s `cpus:` limit on the `reranker` service -- see `reranker/src/libs/model.py`'s `load_model()` docstring.
- The reranker model is loaded eagerly at import time (`server.py`), not lazily -- this service's only job is reranking, so there's no "start fast for a handshake" concern to trade off against slow startup while the checkpoint downloads.

## Both

No env vars are read anywhere except in `docker-compose.yml`/`ollama/entrypoint.sh`/`reranker/src` themselves -- there's no other app code here to keep in sync.
