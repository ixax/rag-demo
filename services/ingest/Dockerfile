FROM python:3.12-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1

COPY services/ingest/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# _common/ lives at services/_common, shared with mcp-server and the
# reranker service (config loading, Ollama embedding client) -- that's why
# this service builds from the repo root context (see docker-compose.yml's
# `context: .` / `dockerfile: services/ingest/Dockerfile`) instead of just
# ./services/ingest.
COPY services/_common/ ./_common/

COPY services/ingest/ingest.py services/ingest/config.yml ./

ENTRYPOINT ["python", "ingest.py"]
