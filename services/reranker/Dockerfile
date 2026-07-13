FROM python:3.12-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1 \
    HF_HOME=/root/.cache/huggingface

COPY services/reranker/requirements.txt .
# torch is CPU-only here (no GPU in this stack) -- pull it from PyTorch's own
# CPU wheel index so pip doesn't grab a multi-GB CUDA build.
RUN pip install --no-cache-dir torch==2.4.1 --index-url https://download.pytorch.org/whl/cpu \
    && pip install --no-cache-dir -r requirements.txt

# _common/ lives at services/_common, shared with ingest and mcp-server
# (config loading) -- that's why this service builds from the repo root
# context (see docker-compose.yml's `context: .` / `dockerfile:
# services/reranker/Dockerfile`) instead of just ./services/reranker.
COPY services/_common/ ./_common/

COPY services/reranker/src/ ./src/
COPY services/reranker/config.yml .

EXPOSE 50051
# CMD (not ENTRYPOINT) so docker-compose's `command:` can freely replace this,
# e.g. append --reload for dev. `src.server:app` is the ASGI app exposed at
# module level in src/server.py. config.yml stays at /app (not /app/src) --
# src/server.py reads it as a plain relative path, which resolves against
# uvicorn's cwd (/app), not the package's own location.
CMD ["uvicorn", "src.server:app", "--host", "0.0.0.0", "--port", "50051"]
