-include .env
export

QDRANT_HTTP_PORT ?= 6333
OPEN_WEBUI_PORT ?= 3000
MCP_SERVER_PORT ?= 8000
PIPELINES_PORT ?= 9099

.PHONY: up down restart status ps logs ingest ingest-force mcp-logs clean monitoring-up monitoring-down monitoring-logs webui-up webui-down webui-logs

up:
	docker compose up -d --build

# --profile is required here even for `down`/`clean` -- compose only stops/
# removes services with no profile assigned unless their profile is passed
# explicitly, so a bare `docker compose down` would leave open-webui/
# pipelines/monitoring containers (if up) running and their volumes busy.
down:
	docker compose --profile open-webui --profile monitoring down

clean:
	docker compose --profile open-webui --profile monitoring down -v

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

# Open WebUI + pipelines -- profile-gated (profiles: [open-webui] in
# docker-compose.yml) so plain `make up`/`docker compose up` never starts
# them; only qdrant/mcp-server (the MCP-only core) come up by default.
webui-up:
	docker compose --profile open-webui up -d --build open-webui open-webui-pipelines

webui-down:
	docker compose --profile open-webui stop open-webui open-webui-pipelines

webui-logs:
	docker compose --profile open-webui logs -f open-webui open-webui-pipelines

# Tempo + Loki + otel-collector + Prometheus + cAdvisor + node-exporter +
# Grafana -- profile-gated (profiles: ["monitoring"] in docker-compose.yml)
# so plain `make up`/`docker compose up` never starts them.
monitoring-up:
	docker compose --profile monitoring up -d tempo loki otel-collector prometheus cadvisor node-exporter grafana

monitoring-down:
	docker compose --profile monitoring stop tempo loki otel-collector prometheus cadvisor node-exporter grafana

monitoring-logs:
	docker compose --profile monitoring logs -f tempo loki otel-collector prometheus cadvisor node-exporter grafana
