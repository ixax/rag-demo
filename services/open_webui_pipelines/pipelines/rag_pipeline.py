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

        data = result.structuredContent or {}
        answer = data.get("answer")
        if answer is None:
            yield "No relevant documents were found in the knowledge base for this query."
            return

        sources = data.get("sources") or []
        if sources:
            yield answer + "\n\nSources:\n" + "\n".join(sources)
        else:
            yield answer
