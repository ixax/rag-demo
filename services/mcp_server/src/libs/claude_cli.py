"""Subscription-billed Claude generation via the headless `claude` CLI
(Claude Code), as a subprocess -- not the `anthropic` SDK/Messages API.

Why: the Messages API (what libs/anthropic.py's SDK client hits) always
bills against an organization's per-token API credit balance, regardless of
whether the request is authenticated with a static API key or an OAuth
token from `ant auth login` -- there is no way to make a raw Messages API
call draw from a Claude Pro/Max/Team chat subscription instead. Only
requests that go through the actual Claude Code client are metered against
that subscription's usage. This module shells out to that real client
(`claude -p`, its non-interactive/headless mode) so search_tools.backend.
type: anthropic_subscription and anthropic_chat's auth: "subscription"
genuinely bill against the subscription, not a token balance.

Auth: the `claude` CLI reads CLAUDE_CODE_OAUTH_TOKEN from the environment
itself (a long-lived token from `claude setup-token`, which requires an
active Claude subscription) -- nothing is passed as an argument here. No
environment variables are otherwise read here -- model/system_prompt/
user_content/timeout all come in as arguments from server.py, which is the
only place that reads env/config (CLAUDE_CODE_OAUTH_TOKEN included).
"""

from __future__ import annotations

import json
import subprocess

from .generation import GenerationResult


class ClaudeCliError(RuntimeError):
    """The `claude` CLI exited non-zero, timed out, or returned
    is_error: true in its JSON result."""


def generate(model: str, system_prompt: str, user_content: str, timeout: float) -> GenerationResult:
    # --tools "": pure text generation, no bash/file/web tool access -- this
    # is a stateless chat completion, not an agent session.
    # --output-format json: structured result (result/stop_reason/usage)
    # instead of parsing bare stdout text -- this is also where token usage
    # comes from, read and returned below rather than discarded.
    # --no-session-persistence: don't write a resumable session to disk --
    # each call is independent, same as a stateless API request.
    args = [
        "claude",
        "-p",
        "--tools",
        "",
        "--output-format",
        "json",
        "--no-session-persistence",
        "--model",
        model,
        "--system-prompt",
        system_prompt,
    ]
    try:
        proc = subprocess.run(args, input=user_content, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError as exc:
        raise ClaudeCliError(
            "claude CLI not found on PATH -- is it installed in this image? (see services/mcp_server/Dockerfile)"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise ClaudeCliError(f"claude CLI timed out after {timeout}s") from exc

    if proc.returncode != 0:
        raise ClaudeCliError(f"claude CLI exited {proc.returncode}: {proc.stderr.strip()}")

    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise ClaudeCliError(f"claude CLI produced non-JSON output: {proc.stdout!r}") from exc

    if payload.get("is_error"):
        raise ClaudeCliError(f"claude CLI reported an error: {payload.get('result') or payload}")

    # usage.input_tokens alone undercounts whenever most of the prompt was
    # served from cache (the common case here, since search_tools.system_
    # prompt/source_template are near-identical across calls) -- cache_
    # creation_input_tokens/cache_read_input_tokens are the rest of what was
    # actually processed as input, so sum all three for a total that means
    # "how much input this call cost", not just the uncached remainder.
    usage = payload.get("usage") or {}
    input_tokens = (
        usage.get("input_tokens", 0) + usage.get("cache_creation_input_tokens", 0) + usage.get("cache_read_input_tokens", 0)
    )
    output_tokens = usage.get("output_tokens", 0)

    return GenerationResult(text=payload["result"], input_tokens=input_tokens, output_tokens=output_tokens)
