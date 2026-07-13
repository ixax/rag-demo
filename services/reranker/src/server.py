"""FastAPI server exposing cross-encoder reranking as a standalone HTTP
service, so consumers (mcp-server) don't need to depend on
sentence-transformers/torch themselves.

This file is the route handler + wiring only -- config loading and model
loading/scoring live in libs/. Env vars and config.yml are read *only*
here; libs/ functions take what they need as arguments.

The model is loaded eagerly at import time (not lazily, unlike mcp-server's
old in-process reranker) -- this service's only job is reranking, so
there's no "start fast for a handshake" concern to trade off against;
slower startup while the checkpoint downloads/loads is the whole point of
splitting this out into its own service instead of blocking mcp-server's
startup. Host/port aren't read here -- uvicorn's `--host`/`--port` CLI
args (see Dockerfile/docker-compose.yml) own binding; the app itself
doesn't need to know its own address.
"""

from __future__ import annotations

import sys
from pathlib import Path

from environs import Env
from fastapi import FastAPI
from pydantic import BaseModel

from .libs.config import RerankerConfig, load_config
from .libs.model import load_model, score

env = Env()
MODEL_RERANKER = env.str("MODEL_RERANKER")

raw_config = load_config(Path("config.yml"))
raw_config.raw["model"] = MODEL_RERANKER
config = raw_config.validate(RerankerConfig)

print(f"loading reranker model {config.model} ...", file=sys.stderr)
_model = load_model(config.model, config.max_length)
print("reranker ready", file=sys.stderr)

app = FastAPI()


class RerankRequest(BaseModel):
    query: str
    documents: list[str]


class RerankResponse(BaseModel):
    # Scores aligned 1:1 with `documents` in the request, in the same order
    # (not sorted) -- the caller sorts/truncates as needed.
    scores: list[float]


@app.post("/rerank")
def rerank(request: RerankRequest) -> RerankResponse:
    return RerankResponse(scores=score(_model, request.query, request.documents))
