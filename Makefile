-include .env
export

QDRANT_HTTP_PORT ?= 6333
OLLAMA_PORT ?= 11434
OPEN_WEBUI_PORT ?= 3000
MCP_SERVER_PORT ?= 8000

.PHONY: up down restart status ps logs pull-models ingest ingest-force mcp-logs clean

up:
	docker compose up -d

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

logs:
	docker compose logs -f

mcp-logs:
	docker compose logs -f mcp-server

pull-models:
	docker compose run --rm ollama-pull

ingest:
	docker compose --profile ingest run --rm --build ingest

ingest-force:
	docker compose --profile ingest run --rm --build -e FORCE_INGEST=true ingest

clean:
	docker compose down -v
