"""Shared base for every AI gateway client (embedding/reasoning/reranker) --
builds the httpx.Client (base_url, timeout, auth header) each one needs, and
posts through it with 429 handling every subclass gets for free.

No environment variables are read here -- url/timeout/credentials all come
in as arguments from each service's own entrypoint, which is the only place
that reads env/config.

api_key/auth_header/auth_value_template have no defaults: which header
carries the key and in what shape is entirely gateway-specific (see
README's Configuration section) -- a default here would silently pick one
gateway's convention over another's.
"""

from __future__ import annotations

import time

import httpx

from _common.logging_config import get_logger

logger = get_logger(__name__)

# Used when a 429 response has no (or an unparseable) Retry-After header --
# the gateway's rate limiter told us to back off but not for how long, so
# this is a short, arbitrary wait before trying again rather than a
# calculated one.
_DEFAULT_RETRY_AFTER_SECONDS = 5.0
# Retry-After is int seconds already elapsed by the time we see the response
# plus network/processing overhead, so a small margin avoids re-hitting the
# limit right as it resets.
_RETRY_AFTER_BUFFER_SECONDS = 1.0

# Model unavailability (connection refused/reset, gateway 502/503/504) isn't
# rate limiting -- there's no Retry-After to honor, and a fixed wait would
# either hammer a still-restarting backend or under-wait a slow one. Backoff
# doubles each attempt instead: 1s, 2s, 4s, 8s, 16s.
_UNAVAILABLE_BACKOFF_BASE_SECONDS = 1.0
_UNAVAILABLE_STATUS_CODES = {502, 503, 504}


class AIGatewayClient:
    def __init__(
        self,
        url: str,
        timeout: float,
        api_key: str,
        auth_header: str,
        auth_value_template: str,
        max_retries: int = 5,
    ) -> None:
        headers = {auth_header: auth_value_template.format(key=api_key)} if api_key else {}
        self._client = httpx.Client(base_url=url, timeout=timeout, headers=headers)
        self._max_retries = max_retries

    def close(self) -> None:
        self._client.close()

    def _raise(self, resp: httpx.Response) -> None:
        """Logs the gateway's response headers/body before raising --
        LiteLLM's own error responses carry trace-correlation headers (and a
        JSON error body with its own call id) that are the way to find the
        matching entry in the gateway's logs, but a bare
        `httpx.HTTPStatusError` only carries the status code and URL."""
        if resp.is_error:
            logger.error(
                "gateway error status=%d for %s: headers=%s body=%s",
                resp.status_code,
                resp.request.url,
                dict(resp.headers),
                resp.text[:2000],
            )
        resp.raise_for_status()

    def _post_with_retry(self, path: str, json: dict) -> httpx.Response:
        """POST json to path, waiting out and retrying 429 (rate limit)
        responses instead of failing the caller's whole run over a transient
        limit the gateway itself told us how to recover from. Model
        unavailability (connection errors, 502/503/504) is retried too, on
        an exponential backoff since there's no Retry-After to honor. Any
        other error status raises immediately, same as a plain
        `resp.raise_for_status()` would."""
        logger.debug("POST %s model=%s", path, json.get("model"))
        attempt = 0
        unavailable_attempt = 0
        while True:
            try:
                resp = self._client.post(path, json=json)
            except httpx.TransportError:
                unavailable_attempt += 1
                if unavailable_attempt > self._max_retries:
                    raise
                wait_seconds = _UNAVAILABLE_BACKOFF_BASE_SECONDS * 2 ** (unavailable_attempt - 1)
                logger.warning(
                    "model unavailable at %s (attempt %d/%d), waiting %.1fs",
                    path,
                    unavailable_attempt,
                    self._max_retries,
                    wait_seconds,
                )
                time.sleep(wait_seconds)
                continue

            if resp.status_code in _UNAVAILABLE_STATUS_CODES:
                unavailable_attempt += 1
                if unavailable_attempt > self._max_retries:
                    self._raise(resp)
                wait_seconds = _UNAVAILABLE_BACKOFF_BASE_SECONDS * 2 ** (unavailable_attempt - 1)
                logger.warning(
                    "model unavailable (status %d) at %s (attempt %d/%d), waiting %.1fs",
                    resp.status_code,
                    path,
                    unavailable_attempt,
                    self._max_retries,
                    wait_seconds,
                )
                time.sleep(wait_seconds)
                continue

            if resp.status_code != 429:
                self._raise(resp)
                return resp
            attempt += 1
            if attempt > self._max_retries:
                self._raise(resp)
            wait_seconds = _DEFAULT_RETRY_AFTER_SECONDS
            retry_after = resp.headers.get("retry-after")
            if retry_after is not None:
                try:
                    wait_seconds = float(retry_after) + _RETRY_AFTER_BUFFER_SECONDS
                except ValueError:
                    pass
            # Grabbed by substring match, not a fixed header name -- gateways
            # vary in what they call these (x-ratelimit-limit-requests,
            # x-litellm-key-remaining-requests-*, ...), and this is only for
            # an operator reading logs, not something the retry logic itself
            # depends on parsing correctly.
            limit_headers = {k: v for k, v in resp.headers.items() if "limit" in k.lower()}
            logger.warning(
                "rate limited by %s (attempt %d/%d), waiting %.1fs%s",
                path,
                attempt,
                self._max_retries,
                wait_seconds,
                f" [{limit_headers}]" if limit_headers else "",
            )
            time.sleep(wait_seconds)
