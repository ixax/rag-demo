"""Shared base for every AI gateway client (embedding/reasoning/reranker) --
builds the httpx.Client (base_url, timeout, auth header) each one needs.

No environment variables are read here -- url/timeout/credentials all come
in as arguments from each service's own entrypoint, which is the only place
that reads env/config.

api_key/auth_header/auth_value_template have no defaults: which header
carries the key and in what shape is entirely gateway-specific (see
README's Configuration section) -- a default here would silently pick one
gateway's convention over another's.
"""

from __future__ import annotations

import httpx


class AIGatewayClient:
    def __init__(
        self,
        url: str,
        timeout: float,
        api_key: str,
        auth_header: str,
        auth_value_template: str,
    ) -> None:
        headers = {auth_header: auth_value_template.format(key=api_key)} if api_key else {}
        self._client = httpx.Client(base_url=url, timeout=timeout, headers=headers)

    def close(self) -> None:
        self._client.close()
