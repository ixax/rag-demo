-include .env
export

.PHONY: up up-gpu down restart status ps logs pull clean

up:
	docker compose up -d --build

up-gpu:
	docker compose -f docker-compose.yml -f docker-compose.gpu.yml up -d --build

down:
	docker compose down

clean:
	docker compose down -v

restart: down up

status ps:
	docker compose ps

logs:
	docker compose logs -f

pull:
	docker compose run --rm ollama-pull
