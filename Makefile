-include .env
export

QDRANT_HTTP_PORT ?= 6333
OLLAMA_PORT ?= 11434
OPEN_WEBUI_PORT ?= 3000

.PHONY: up down restart status ps logs pull-models clean

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

logs:
	docker compose logs -f

pull-models:
	docker compose run --rm ollama-pull

clean:
	docker compose down -v
