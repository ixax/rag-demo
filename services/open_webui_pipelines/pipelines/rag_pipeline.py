"""
title: RAG (UE Docs)
author: rag-stack
version: 1.0
license: MIT
description: Open WebUI pipe that answers questions by calling this repo's mcp-server (answer_question tool) directly over streamable-HTTP MCP, instead of relying on a chat model's tool-calling. Selectable as its own model in the Open WebUI model picker.
requirements: mcp==1.28.1, starlette==0.37.2, environs==15.0.1
"""

from __future__ import annotations

import asyncio
import json
from typing import Generator, Iterator, List, Union

from environs import Env
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from pydantic import BaseModel

# env.str(name) raises a clear environs.EnvError if unset -- no default,
# same fail-fast pattern as services/mcp_server/src/server.py.
env = Env()
RAG_MCP_URL = env.str("RAG_MCP_URL")


class Pipeline:
    class Valves(BaseModel):
        MCP_URL: str

    def __init__(self):
        self.id = "rag-ue"
        self.name = "RAG (UE Docs)"
        self.valves = self.Valves(MCP_URL=RAG_MCP_URL)

    async def on_startup(self):
        pass

    async def on_shutdown(self):
        pass

    async def _call_answer_question(self, query: str) -> dict:
        # No top_k passed -- answer_question already defaults it to
        # mcp-server's own config.yml (retrieval.top_k_rerank); this
        # pipeline doesn't second-guess that tuning knob.
        async with streamablehttp_client(self.valves.MCP_URL) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                return await session.call_tool("answer_question", {"query": query})

    def pipe(
        self, user_message: str, model_id: str, messages: List[dict], body: dict
    ) -> Union[str, Generator, Iterator]:
        # Run synchronously (not `async def pipe`) -- this pipelines runtime
        # doesn't await an async pipe(), it just logs "coroutine was never
        # awaited" and returns nothing. asyncio.run() here drives the async
        # MCP client (streamable-http) without needing the framework's
        # cooperation.
        result = asyncio.run(self._call_answer_question(user_message))

        if result.isError:
            error_text = "\n".join(getattr(block, "text", str(block)) for block in result.content)
            yield f"mcp-server error: {error_text}"
            return

        # FastMCP doesn't populate structuredContent for a bare `-> dict`
        # return type (no output schema to validate against) -- the actual
        # payload only ever arrives as a JSON-encoded TextContent block in
        # result.content, even though a naive `or {}` fallback on
        # structuredContent silently "succeeds" with an empty dict instead
        # of erroring, which is what made this so easy to miss.
        data = json.loads(result.content[0].text) if result.content else {}
        answer = data.get("answer")
        if answer is None:
            yield "No relevant documents were found in the knowledge base for this query."
            return

        parts = [answer]

        reasoning = data.get("reasoning")
        if reasoning:
            parts.append(f"Reasoning: {reasoning}")

        sources = data.get("sources") or []
        if sources:
            parts.append("Sources:\n" + "\n".join(sources))

        input_tokens = data.get("input_tokens")
        output_tokens = data.get("output_tokens")
        if input_tokens is not None and output_tokens is not None:
            parts.append(f"Tokens: {input_tokens} in / {output_tokens} out")

        trace = data.get("trace") or []
        if trace:
            steps = ", ".join(f"{s['step']} {s['duration_ms']}ms" for s in trace)
            total_ms = sum(s["duration_ms"] for s in trace)
            parts.append(f"Timings: {steps} (total {total_ms:.0f}ms)")

        trace_id = data.get("trace_id")
        if trace_id:
            parts.append(f"Trace ID: {trace_id}")

        yield "\n\n".join(parts)
