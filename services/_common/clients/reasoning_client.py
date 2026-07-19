"""Reasoning (chat generation) client -- mcp-server's answer_question
generation step. Not used by ingest.
"""

from __future__ import annotations

from dataclasses import dataclass

from .ai_gateway_client import AIGatewayClient


@dataclass(frozen=True)
class GenerationResult:
    text: str
    # Always 0 -- token accounting for this backend isn't tracked, by design
    # (it's the free/local path).
    input_tokens: int
    output_tokens: int


class ReasoningClient(AIGatewayClient):
    """Speaks the OpenAI-compatible surface (/v1/chat/completions) the AI
    gateway exposes in front of whatever backing model it routes to."""

    def generate(
        self,
        model: str,
        system_prompt: str,
        user_content: str,
        options: dict[str, float] | None = None,
        response_schema: dict | None = None,
    ) -> GenerationResult:
        body: dict = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
            "stream": False,
        }
        # Both optional/omitted by default so a caller that doesn't pass
        # them (or a config.yml search_tools.generation without options/
        # response_schema set) gets the gateway's own defaults rather than
        # this method injecting empty values. options -> sampler params
        # (e.g. temperature, min_p), sent as top-level fields -- the
        # OpenAI-compatible surface has no nested "options" envelope; the
        # gateway forwards unrecognized fields straight through to the
        # backing model. response_schema -> OpenAI's
        # response_format.json_schema, which constrains sampling to that
        # JSON shape at the grammar level instead of a textual "reply in
        # this format" instruction.
        if options is not None:
            body.update(options)
        if response_schema is not None:
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "response", "schema": response_schema, "strict": True},
            }
        resp = self._client.post("/v1/chat/completions", json=body)
        resp.raise_for_status()
        return GenerationResult(
            text=resp.json()["choices"][0]["message"]["content"], input_tokens=0, output_tokens=0
        )
