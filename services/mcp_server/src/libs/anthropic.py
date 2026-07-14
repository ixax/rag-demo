"""Anthropic Messages API (SDK) calls -- token-billed generation only.

Used for search_tools.backend.type: "anthropic_token" and anthropic_chat's
auth: "token". Subscription-billed generation (anthropic_subscription /
auth: "subscription") does not go through this module or the SDK at all --
see libs/claude_cli.py for why and how.

No environment variables are read here -- api_key/model/max_tokens all come
in as arguments from server.py, which is the only place that reads
env/config (ANTHROPIC_API_KEY included).
"""

from __future__ import annotations

import anthropic

from .generation import GenerationResult


def build_client(api_key: str) -> anthropic.Anthropic:
    return anthropic.Anthropic(api_key=api_key)


def generate(
    client: anthropic.Anthropic,
    model: str,
    max_tokens: int,
    system_prompt: str,
    user_content: str,
) -> GenerationResult:
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
        text = (
            "Reasoning: request declined by the model's safety filters.\n\n"
            "Answer: The model declined to answer this query."
        )
    else:
        text = "".join(block.text for block in resp.content if block.type == "text")
    return GenerationResult(text=text, input_tokens=resp.usage.input_tokens, output_tokens=resp.usage.output_tokens)
