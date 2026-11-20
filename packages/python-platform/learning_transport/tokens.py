"""a service account's own access token (oauth client credentials), cached until shortly before it expires."""

import asyncio
import time

import httpx

from learning_transport.errors import UpstreamError

_now = time.monotonic  # tests patch this

# seconds: a token is replaced this long before it expires, or halfway through a shorter one.
EXPIRY_MARGIN = 30
FETCH_TIMEOUT = 10  # seconds to wait for keycloak
# seconds a failed fetch is remembered.
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
        """the cached token, or a new one."""
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
            self._token, self._expires = token, _now() + lifetime - min(EXPIRY_MARGIN, lifetime / 2)
            return token

    def reset(self, token: str | None = None) -> None:
        """forget the refused token, or the cached one when unknown, unless it was already replaced."""
        if token is None or self._token == token:
            self._token = None
