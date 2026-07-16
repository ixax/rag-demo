-include .env
export

QDRANT_HTTP_PORT ?= 6333
OLLAMA_PORT ?= 11434
OPEN_WEBUI_PORT ?= 3000
MCP_SERVER_PORT ?= 8000
RERANKER_PORT ?= 50051
PIPELINES_PORT ?= 9099

.PHONY: up up-gpu models-up models-down down restart status ps logs models-pull ingest ingest-force mcp-logs reranker-logs reranker-up reranker-down clean monitoring-up monitoring-down monitoring-logs

up:
	docker compose up -d --build

# For hosts with an NVIDIA GPU reachable from Docker (Windows + Docker
# Desktop/WSL2, or Linux with the NVIDIA Container Toolkit) -- see
# docker-compose.gpu.yml for requirements. Not applicable on macOS.
up-gpu:
	docker compose -f docker-compose.yml -f docker-compose.gpu.yml up -d

down:
	docker compose down

clean:
	docker compose down -v

restart: down up

status ps:
	docker compose ps

logs:
	docker compose logs -f

# Local Ollama + its model-pull job -- profile-gated (profiles: ["models"]
# in docker-compose.yml) so plain `make up` never starts them. Skip this
# entirely when OLLAMA_HOST (.env) points at an external Ollama instance
# instead.
models-up:
	docker compose --profile models up -d ollama ollama-pull

models-pull:
	docker compose --profile models run --rm ollama-pull

models-down:
	docker compose --profile models stop ollama

# Reranker -- profile-gated (profiles: ["reranker"] in docker-compose.yml)
# so plain `make up` never starts it either; run/stop it independently of
# the rest of the stack, e.g. on a separate host, and point RERANKER_HOST
# (.env) at it instead of running it here.
reranker-up:
	docker compose --profile reranker up -d --build reranker

reranker-down:
	docker compose --profile reranker stop reranker

mcp-logs:
	docker compose logs -f mcp-server

reranker-logs:
	docker compose logs -f reranker

ingest:
	docker compose --profile ingest run --rm --build ingest

ingest-force:
	docker compose --profile ingest run --rm --build -e FORCE_INGEST=true ingest

# Tempo + Loki + otel-collector + Prometheus + cAdvisor + node-exporter +
# Grafana -- profile-gated (profiles: ["monitoring"] in docker-compose.yml)
# so plain `make up`/`docker compose up` never starts them.
monitoring-up:
	docker compose --profile monitoring up -d tempo loki otel-collector prometheus cadvisor node-exporter grafana

monitoring-down:
	docker compose --profile monitoring stop tempo loki otel-collector prometheus cadvisor node-exporter grafana

monitoring-logs:
	docker compose --profile monitoring logs -f tempo loki otel-collector prometheus cadvisor node-exporter grafana
