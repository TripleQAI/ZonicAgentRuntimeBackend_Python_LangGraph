"""Authentication: Cognito access tokens verified against the pool's JWKS (design 14.3, decision 0004).

* No token: ``AUTH_REQUIRED``; a bad signature, issuer, audience or an expired token:
  ``AUTH_INVALID`` (case A-02).
* The audience is the app client: an ID token's ``aud``, or an access token's ``client_id``
  (Cognito access tokens carry no ``aud``).
* Keys come from the JWKS URL through ``httpx`` (PyJWT's own client blocks the event loop) and
  are cached for ``auth.jwksCacheSeconds``; an unknown ``kid`` refreshes them once.
* Claim names come from ``settings.auth.claims``.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from typing import Any, Protocol

import httpx
import jwt
from jwt import PyJWKSet

from zonic_runtime.application.caller import Caller
from zonic_runtime.errors import ZonicError
from zonic_runtime.models.resources.settings import AuthSettings

ALGORITHMS = ("RS256",)
_BEARER = "bearer "


class SigningKeys(Protocol):
    async def key(self, kid: str | None) -> Any: ...


class StaticKeys:
    """Keys by ``kid``, for tests and local development with a test issuer."""

    def __init__(self, keys: Mapping[str, Any]) -> None:
        self._keys = dict(keys)

    async def key(self, kid: str | None) -> Any:
        found = self._keys.get(kid or "")
        if found is None:
            raise ZonicError("AUTH_INVALID", reason="unknown key")
        return found


class JwksKeys:
    def __init__(
        self,
        url: str,
        cache_seconds: float,
        client: httpx.AsyncClient | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._url = url
        self._cache_seconds = cache_seconds
        self._client = client or httpx.AsyncClient(timeout=10.0)
        self._monotonic = monotonic
        self._keys: dict[str, Any] = {}
        self._fetched = -float("inf")

    async def _refresh(self) -> None:
        response = await self._client.get(self._url)
        response.raise_for_status()
        found = PyJWKSet.from_dict(response.json())
        self._keys = {k.key_id: k.key for k in found.keys if k.key_id}
        self._fetched = self._monotonic()

    async def key(self, kid: str | None) -> Any:
        stale = self._monotonic() - self._fetched >= self._cache_seconds
        if stale or (kid or "") not in self._keys:
            try:
                await self._refresh()
            except (httpx.HTTPError, jwt.PyJWTError, ValueError) as exc:
                raise ZonicError("AUTH_INVALID", reason="keys unavailable") from exc
        found = self._keys.get(kid or "")
        if found is None:
            raise ZonicError("AUTH_INVALID", reason="unknown key")
        return found


def bearer(header: str | None) -> str:
    if not header or not header.lower().startswith(_BEARER) or not header[len(_BEARER) :].strip():
        raise ZonicError("AUTH_REQUIRED")
    return header[len(_BEARER) :].strip()


class TokenVerifier:
    def __init__(
        self, keys: SigningKeys, settings: AuthSettings, *, issuer: str | None, audience: str | None
    ) -> None:
        self._keys = keys
        self._settings = settings
        self._issuer = issuer
        self._audience = audience

    async def verify(self, token: str) -> Caller:
        try:
            header = jwt.get_unverified_header(token)
            key = await self._keys.key(header.get("kid"))
            claims: dict[str, Any] = jwt.decode(
                token,
                key,
                algorithms=list(ALGORITHMS),
                issuer=self._issuer,
                leeway=self._settings.clock_skew_seconds,
                options={"require": ["exp"], "verify_aud": False, "verify_iss": self._issuer is not None},
            )
        except jwt.PyJWTError as exc:
            raise ZonicError("AUTH_INVALID", reason=type(exc).__name__) from exc
        if self._audience is not None:
            audience = claims.get("aud", claims.get("client_id"))
            accepted = audience if isinstance(audience, list) else [audience]
            if self._audience not in accepted:
                raise ZonicError("AUTH_INVALID", reason="audience")
        names = self._settings.claims
        subject = claims.get(names.subject)
        if not isinstance(subject, str) or not subject:
            raise ZonicError("AUTH_INVALID", reason="subject")
        groups = claims.get(names.groups) or []
        return Caller(
            subject=subject,
            tenant=_text(claims.get(names.tenant)),
            email=_text(claims.get(names.email)),
            groups=frozenset(
                g for g in (groups if isinstance(groups, list) else [groups]) if isinstance(g, str)
            ),
            claims=claims,
        )


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None
