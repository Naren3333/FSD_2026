"""A service account's own access token (OAuth client credentials), cached until shortly before it expires."""

import asyncio
import time

import httpx

from learning_transport.errors import UpstreamError

EXPIRY_MARGIN = 30  # seconds: a token is replaced this long before it expires


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
        self._lock = asyncio.Lock()

    async def get(self) -> str:
        """The cached token, or a new one. Raises UpstreamError("keycloak", 0, "TOKEN_UNAVAILABLE")."""
        async with self._lock:
            if self._token and time.monotonic() < self._expires:
                return self._token
            try:
                resp = await self.http.post(self.token_url, data=self.form, timeout=10)
                resp.raise_for_status()
                data = resp.json()
                token, lifetime = str(data["access_token"]), int(data.get("expires_in", 60))
            except (httpx.HTTPError, ValueError, KeyError, TypeError):
                raise UpstreamError("keycloak", 0, "TOKEN_UNAVAILABLE") from None
            self._token, self._expires = token, time.monotonic() + lifetime - EXPIRY_MARGIN
            return token

    def reset(self) -> None:
        """Forget the token after a service refused it, so the next call fetches a new one."""
        self._token = None
