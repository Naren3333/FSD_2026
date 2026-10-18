"""A service account's own access token (OAuth client credentials), cached until shortly before it expires."""

import asyncio
import time

import httpx

from learning_transport.errors import UpstreamError

_now = time.monotonic  # tests patch this

EXPIRY_MARGIN = 30  # seconds: a token is replaced this long before it expires
FETCH_TIMEOUT = 10  # seconds to wait for Keycloak
# Seconds a failed fetch is remembered. Calls queued behind a fetch that hung for FETCH_TIMEOUT then fail at once
# instead of each waiting as long again in turn.
FAILURE_TTL = 5


class ServiceToken:
    def __init__(self, http: httpx.AsyncClient, token_url: str, client_id: str, client_secret: str) -> None:
        self.http, self.token_url = http, token_url
        self.form = {
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
        }
        self._token: str | None = None
        self._expires = 0.0
        self._failed_until = 0.0
        self._lock = asyncio.Lock()

    async def get(self) -> str:
        """The cached token, or a new one. Raises UpstreamError("keycloak", 0, "TOKEN_UNAVAILABLE"), also without
        asking Keycloak again for FAILURE_TTL seconds after a fetch failed."""
        async with self._lock:
            now = _now()
            if self._token and now < self._expires:
                return self._token
            if now < self._failed_until:
                raise UpstreamError("keycloak", 0, "TOKEN_UNAVAILABLE")
            try:
                resp = await self.http.post(self.token_url, data=self.form, timeout=FETCH_TIMEOUT)
                resp.raise_for_status()
                data = resp.json()
                token, lifetime = str(data["access_token"]), int(data.get("expires_in", 60))
            except (httpx.HTTPError, ValueError, KeyError, TypeError):
                self._failed_until = _now() + FAILURE_TTL
                raise UpstreamError("keycloak", 0, "TOKEN_UNAVAILABLE") from None
            self._token, self._expires = token, _now() + lifetime - EXPIRY_MARGIN
            return token

    def reset(self) -> None:
        """Forget the token after a service refused it, so the next call fetches a new one."""
        self._token = None
