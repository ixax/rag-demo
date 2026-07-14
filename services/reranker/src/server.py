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

from pathlib import Path

from environs import Env
from fastapi import FastAPI
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from pydantic import BaseModel

from _common.logging_config import configure_logging, get_logger
from _common.tracing import configure_tracing, get_tracer

from .libs.config import RerankerConfig, load_config
from .libs.model import load_model, score

env = Env()
MODEL_RERANKER = env.str("MODEL_RERANKER")

configure_logging()
logger = get_logger(__name__)

configure_tracing("reranker")
tracer = get_tracer(__name__)

raw_config = load_config(Path("config.yml"))
raw_config.raw["model"] = MODEL_RERANKER
config = raw_config.validate(RerankerConfig)

logger.info("loading reranker model %s ...", config.model)
_model = load_model(config.model, config.max_length)
logger.info("reranker ready")

app = FastAPI()
# Extracts the incoming W3C trace-context header (mcp-server's httpx client
# is instrumented too -- see _common.tracing.configure_tracing) so this
# service's spans nest under the caller's "rerank" step instead of starting
# a disconnected trace, and auto-creates a server span per request.
FastAPIInstrumentor.instrument_app(app)


class RerankRequest(BaseModel):
    query: str
    documents: list[str]


class RerankResponse(BaseModel):
    # Scores aligned 1:1 with `documents` in the request, in the same order
    # (not sorted) -- the caller sorts/truncates as needed.
    scores: list[float]


@app.post("/rerank")
def rerank(request: RerankRequest) -> RerankResponse:
    with tracer.start_as_current_span("score") as span:
        span.set_attribute("rag.model", config.model)
        span.set_attribute("rag.num_documents", len(request.documents))
        return RerankResponse(scores=score(_model, request.query, request.documents))
