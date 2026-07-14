-include .env
export

QDRANT_HTTP_PORT ?= 6333
OLLAMA_PORT ?= 11434
OPEN_WEBUI_PORT ?= 3000
MCP_SERVER_PORT ?= 8000
RERANKER_PORT_HOST ?= 50051
PIPELINES_PORT ?= 9099

.PHONY: up up-gpu down restart status ps logs pull-models ingest ingest-force mcp-logs reranker-logs clean monitoring-up monitoring-down monitoring-logs

up:
	docker compose up -d

# For hosts with an NVIDIA GPU reachable from Docker (Windows + Docker
# Desktop/WSL2, or Linux with the NVIDIA Container Toolkit) -- see
# docker-compose.gpu.yml for requirements. Not applicable on macOS.
up-gpu:
	docker compose -f docker-compose.yml -f docker-compose.gpu.yml up -d

down:
	docker compose down

restart: down up

status ps:
	@docker compose ps
	@echo ""
	@echo "Health checks:"
	@curl -sf http://localhost:$(QDRANT_HTTP_PORT)/collections >/dev/null \
		&& echo "  qdrant:      OK  (http://localhost:$(QDRANT_HTTP_PORT))" \
		|| echo "  qdrant:      NOT RESPONDING"
	@curl -sf http://localhost:$(OLLAMA_PORT)/api/tags >/dev/null \
		&& echo "  ollama:      OK  (http://localhost:$(OLLAMA_PORT))" \
		|| echo "  ollama:      NOT RESPONDING"
	@curl -sf http://localhost:$(OPEN_WEBUI_PORT)/ >/dev/null \
		&& echo "  open-webui:  OK  (http://localhost:$(OPEN_WEBUI_PORT))" \
		|| echo "  open-webui:  NOT RESPONDING"
	@bash -c '</dev/tcp/localhost/$(MCP_SERVER_PORT)' 2>/dev/null \
		&& echo "  mcp-server:  OK  (http://localhost:$(MCP_SERVER_PORT)/mcp)" \
		|| echo "  mcp-server:  NOT RESPONDING"
	@bash -c '</dev/tcp/localhost/$(RERANKER_PORT_HOST)' 2>/dev/null \
		&& echo "  reranker:    OK  (http://localhost:$(RERANKER_PORT_HOST))" \
		|| echo "  reranker:    NOT RESPONDING"
	@curl -sf http://localhost:$(PIPELINES_PORT)/ >/dev/null \
		&& echo "  pipelines:   OK  (http://localhost:$(PIPELINES_PORT))" \
		|| echo "  pipelines:   NOT RESPONDING"

logs:
	docker compose logs -f

mcp-logs:
	docker compose logs -f mcp-server

reranker-logs:
	docker compose logs -f reranker

pull-models:
	docker compose run --rm ollama-pull

ingest:
	docker compose --profile ingest run --rm --build ingest

ingest-force:
	docker compose --profile ingest run --rm --build -e FORCE_INGEST=true ingest

clean:
	docker compose down -v

# Tempo + Loki + otel-collector + Prometheus + cAdvisor + node-exporter +
# Grafana -- profile-gated (profiles: ["monitoring"] in docker-compose.yml)
# so plain `make up`/`docker compose up` never starts them.
monitoring-up:
	docker compose --profile monitoring up -d tempo loki otel-collector prometheus cadvisor node-exporter grafana

monitoring-down:
	docker compose --profile monitoring stop tempo loki otel-collector prometheus cadvisor node-exporter grafana

monitoring-logs:
	docker compose --profile monitoring logs -f tempo loki otel-collector prometheus cadvisor node-exporter grafana
