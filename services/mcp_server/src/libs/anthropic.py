"""Anthropic-specific calls: client construction and chat generation.

No environment variables are read here -- api_key/model/max_tokens all come
in as arguments from server.py, which is the only place that reads
env/config (ANTHROPIC_API_KEY included).
"""

from __future__ import annotations

import anthropic


def build_client(api_key: str) -> anthropic.Anthropic:
    return anthropic.Anthropic(api_key=api_key)


def generate(
    client: anthropic.Anthropic,
    model: str,
    max_tokens: int,
    system_prompt: str,
    user_content: str,
) -> str:
    # Thinking omitted (off) -- this is a direct grounded-QA task, not
    # open-ended reasoning, and the system prompt already asks the model for
    # an explicit Reasoning section, so adaptive thinking would just
    # duplicate that at the cost of latency.
    resp = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=system_prompt,
        messages=[{"role": "user", "content": user_content}],
    )
    if resp.stop_reason == "refusal":
        return (
            "Reasoning: request declined by the model's safety filters.\n\n"
            "Answer: The model declined to answer this query."
        )
    return "".join(block.text for block in resp.content if block.type == "text")
