FROM python:3.12-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1

# Node.js + the `claude` CLI (Claude Code) -- needed only for
# search_tools.backend.type: anthropic_subscription and/or anthropic_chat's
# auth: "subscription" (see src/libs/claude_cli.py's docstring for why
# subscription-billed generation shells out to this instead of using the
# Anthropic SDK). Installed unconditionally since config.yml is read at
# container runtime, not image build time -- a token-only/ollama-only
# deployment carries this image weight unused.
RUN apt-get update && apt-get install -y --no-install-recommends curl gnupg \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && npm install -g @anthropic-ai/claude-code \
    && apt-get purge -y --auto-remove curl gnupg \
    && rm -rf /var/lib/apt/lists/*

COPY services/mcp_server/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# _common/ lives at services/_common, shared with ingest and the reranker
# service (config loading, Ollama embedding client) -- that's why this
# service builds from the repo root context (see docker-compose.yml's
# `context: .` / `dockerfile: services/mcp_server/Dockerfile`) instead of
# just ./services/mcp_server.
COPY services/_common/ ./_common/

COPY services/mcp_server/src/ ./src/
COPY services/mcp_server/config.yml .

EXPOSE 8000
# CMD (not ENTRYPOINT) so docker-compose's `command:` can freely replace this,
# e.g. append --reload for dev. `src.server:app` is the ASGI app exposed at
# module level in src/server.py -- running it through plain uvicorn (rather
# than calling mcp.run() from __main__) is what makes --reload possible.
# config.yml stays at /app (not /app/src) -- src/server.py reads it as a
# plain relative path, which resolves against uvicorn's cwd (/app), not the
# package's own location.
CMD ["uvicorn", "src.server:app", "--host", "0.0.0.0", "--port", "8000"]
