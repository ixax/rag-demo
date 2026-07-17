-include .env
export

QDRANT_HTTP_PORT ?= 6333
OPEN_WEBUI_PORT ?= 3000
MCP_SERVER_PORT ?= 8000
PIPELINES_PORT ?= 9099

.PHONY: up down restart status ps logs ingest ingest-force mcp-logs clean monitoring-up monitoring-down monitoring-logs

up:
	docker compose up -d --build

down:
	docker compose down

clean:
	docker compose down -v

restart: down up

status ps:
	docker compose ps

logs:
	docker compose logs -f

mcp-logs:
	docker compose logs -f mcp-server

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
