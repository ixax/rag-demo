#!/usr/bin/env python3
"""Pre-flight health check for the RAG stack's two live HTTP dependencies
(Qdrant, AI gateway). Run inside the mcp-server container, where the real
AI_GATEWAY_URL/AI_GATEWAY_API_KEY/etc env vars and network routes (qdrant,
host.docker.internal) already exist:

    docker compose exec -T mcp-server python3 - < scripts/health_check.py

Exit code 0 iff every check passes.
"""

from __future__ import annotations

import os
import sys
import urllib.error
import urllib.request

QDRANT_URL = "http://qdrant:6333/collections"
QDRANT_TIMEOUT = 3.0
AI_GATEWAY_TIMEOUT = 5.0


def check_qdrant() -> tuple[bool, str]:
    collection = os.environ.get("QDRANT_COLLECTION", "")
    try:
        with urllib.request.urlopen(QDRANT_URL, timeout=QDRANT_TIMEOUT) as resp:
            body = resp.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, OSError) as exc:
        return False, f"unreachable at {QDRANT_URL}: {exc}"
    if collection and collection not in body:
        return False, f"reachable but collection '{collection}' not found in response"
    return True, f"reachable, collection '{collection}' present" if collection else "reachable"


def check_ai_gateway() -> tuple[bool, str]:
    base_url = os.environ.get("AI_GATEWAY_URL", "")
    api_key = os.environ.get("AI_GATEWAY_API_KEY", "")
    auth_header = os.environ.get("AI_GATEWAY_AUTH_HEADER", "")
    auth_value_template = os.environ.get("AI_GATEWAY_AUTH_VALUE_TEMPLATE", "")
    if not base_url:
        return False, "AI_GATEWAY_URL is not set"
    url = f"{base_url.rstrip('/')}/v1/models"
    headers = {}
    if api_key and auth_header and auth_value_template:
        headers[auth_header] = auth_value_template.format(key=api_key)
    req = urllib.request.Request(url, headers=headers)
    try:
        urllib.request.urlopen(req, timeout=AI_GATEWAY_TIMEOUT)
    except urllib.error.HTTPError as exc:
        return False, f"HTTP {exc.code} from {url}"
    except (urllib.error.URLError, OSError) as exc:
        return False, f"unreachable at {url}: {exc}"
    return True, "reachable"


def main() -> int:
    checks = [
        ("qdrant", check_qdrant),
        ("ai-gateway", check_ai_gateway),
    ]
    all_passed = True
    for name, check in checks:
        passed, detail = check()
        all_passed &= passed
        print(f"[{'PASS' if passed else 'FAIL'}] {name}: {detail}")
    return 0 if all_passed else 1


if __name__ == "__main__":
    sys.exit(main())
