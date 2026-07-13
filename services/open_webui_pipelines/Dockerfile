FROM ghcr.io/open-webui/pipelines:main

# rag_pipeline.py's `requirements:` frontmatter names these same pins --
# pipelines' loader re-runs `pip install` against that frontmatter on every
# module load regardless, but with the versions already satisfied here it's
# a no-op instead of re-resolving (and fighting the base image's own
# starlette/sse-starlette pins) on every container start.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Bakes the pipeline scripts into the image so it's self-contained; compose
# bind-mounts ./pipelines over this directory for dev-loop live editing
# (same convention as mcp-server/reranker's src/ -- see AGENTS.md).
COPY pipelines/ ./pipelines/
