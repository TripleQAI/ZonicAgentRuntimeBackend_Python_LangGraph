"""``EgressGuard``: the egress port over ``httpx`` with ``egress.json`` (plan P3-03; cases N-14, X-02).

For every request, and again for every redirect it follows:

1. The scheme, the host (exact, or ``*.suffix``) and the port must be on the allow list.
2. The host is resolved, and with ``blockPrivateRanges`` every address must be public: private,
   loopback, link-local (the cloud metadata address), multicast, reserved and unspecified
   addresses are ``EGRESS_DENIED``, and so is a host that resolves to nothing.
3. The connection goes to the address just checked, never to a second lookup (no DNS
   rebinding): the URL carries the address, the ``Host`` header and TLS server name carry the
   host, so the certificate is still verified against the host.
4. Redirects are followed only with ``followRedirects``, at most ``_MAX_REDIRECTS``, each checked
   as a new request; otherwise a 3xx answer is returned as it is.
5. A response above ``maxResponseBytes`` is ``EGRESS_DENIED``; the timeout is the request's,
   capped by ``timeoutSeconds``.

Environment proxies are ignored (``trust_env=False``), so no setting outside ``egress.json``
can send a call elsewhere.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import socket
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from zonic_runtime.errors import ZonicError
from zonic_runtime.models.resources.catalogs import EgressPolicy
from zonic_runtime.ports.egress import EgressRequest, EgressResponse

DENIED = "EGRESS_DENIED"
TIMEOUT = "TIMEOUT"
UPSTREAM = "UPSTREAM_5XX"
_MAX_REDIRECTS = 5
_REDIRECTS = frozenset({301, 302, 303, 307, 308})
_DEFAULT_PORTS = {"https": 443, "http": 80}
#: ``(host, port) -> addresses``.
Resolver = Callable[[str, int], Awaitable[list[str]]]


async def system_resolver(host: str, port: int) -> list[str]:
    infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return sorted({str(info[4][0]) for info in infos})


def _public(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return not (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
    )


def _ip(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return None


class EgressGuard:
    def __init__(
        self,
        policy: EgressPolicy,
        *,
        resolver: Resolver = system_resolver,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._policy = policy
        self._resolver = resolver
        self._transport = transport
        self._client: httpx.AsyncClient | None = None
        self._hosts = frozenset(h.lower() for h in policy.hosts if not h.startswith("*."))
        self._suffixes = tuple(h[1:].lower() for h in policy.hosts if h.startswith("*."))

    def _http(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                follow_redirects=False, trust_env=False, transport=self._transport
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()

    # -- the checks ------------------------------------------------------------------------------

    def _allowed_host(self, host: str) -> bool:
        name = host.lower().rstrip(".")
        return name in self._hosts or any(name.endswith(s) for s in self._suffixes)

    def _listed(self, url: httpx.URL) -> tuple[str, int]:
        """The host and port when the scheme, host and port are on the allow list."""
        scheme, host = url.scheme.lower(), (url.host or "").lower()
        port = url.port or _DEFAULT_PORTS.get(scheme)
        if scheme not in self._policy.schemes:
            raise ZonicError(DENIED, reason="scheme", scheme=scheme)
        if not host or not self._allowed_host(host):
            raise ZonicError(DENIED, reason="host", host=host)
        if port is None or port not in self._policy.ports:
            raise ZonicError(DENIED, reason="port", host=host, port=port)
        return host, port

    def allows(self, url: str) -> bool:
        """Whether the allow list admits ``url`` (a ``webhook`` endpoint, checked when bindings are
        resolved); addresses are still checked when a request is sent."""
        try:
            self._listed(httpx.URL(url))
        except (ZonicError, httpx.InvalidURL):
            return False
        return True

    async def check(self, url: httpx.URL) -> str:
        """The address to connect to; ``EGRESS_DENIED`` for anything the policy does not allow."""
        host, port = self._listed(url)
        literal = _ip(host)
        try:
            addresses = [host] if literal is not None else await self._resolver(host, port)
        except OSError as exc:
            raise ZonicError(DENIED, reason="unresolved", host=host) from exc
        if not addresses:
            raise ZonicError(DENIED, reason="unresolved", host=host)
        if self._policy.block_private_ranges:
            for address in addresses:
                ip = _ip(address)
                if ip is None or not _public(ip):
                    raise ZonicError(DENIED, reason="address", host=host, address=address)
        return addresses[0]

    # -- sending ---------------------------------------------------------------------------------

    async def send(self, request: EgressRequest) -> EgressResponse:
        method = request.method.upper()
        timeout = min(request.timeout_seconds or self._policy.timeout_seconds, self._policy.timeout_seconds)
        try:
            url = httpx.URL(request.url)
        except httpx.InvalidURL as exc:
            raise ZonicError(DENIED, reason="url") from exc
        body = request.body
        headers = list(request.headers)
        for _ in range(_MAX_REDIRECTS + 1):
            response = await self._once(method, url, headers, body, timeout)
            location = response.headers.get("location")
            if response.status not in _REDIRECTS or not self._policy.follow_redirects or not location:
                return response
            url = url.join(location)
            if response.status == 303 or (response.status in (301, 302) and method == "POST"):
                method, body = "GET", None
        raise ZonicError(DENIED, reason="redirects", limit=_MAX_REDIRECTS)

    async def _once(
        self, method: str, url: httpx.URL, headers: list[tuple[str, str]], body: Any, seconds: float
    ) -> EgressResponse:
        address = await self.check(url)
        host = url.host or ""
        pinned = url.copy_with(host=address)
        sent = [(k, v) for k, v in headers if k.lower() != "host"]
        sent.append(("host", url.netloc.decode("ascii")))
        content: bytes | None
        if body is None or body == "":
            content = None
        elif isinstance(body, (bytes, str)):
            content = body.encode("utf-8") if isinstance(body, str) else body
        else:
            content = json.dumps(body, ensure_ascii=False).encode("utf-8")
            if not any(k.lower() == "content-type" for k, _ in sent):
                sent.append(("content-type", "application/json"))
        extensions = {"sni_hostname": host} if url.scheme == "https" else {}
        try:
            async with asyncio.timeout(seconds):
                built = self._http().build_request(
                    method, pinned, headers=sent, content=content, extensions=extensions
                )
                answer = await self._http().send(built, stream=True)
                try:
                    data = await self._read(answer, host)
                finally:
                    await answer.aclose()
        except TimeoutError as exc:
            raise ZonicError(TIMEOUT, method=method, host=host, seconds=seconds) from exc
        except httpx.TimeoutException as exc:
            raise ZonicError(TIMEOUT, method=method, host=host, seconds=seconds) from exc
        except httpx.TransportError as exc:
            raise ZonicError(UPSTREAM, method=method, host=host, reason="connection") from exc
        if answer.status_code >= 500:
            raise ZonicError(UPSTREAM, method=method, host=host, status=answer.status_code)
        found: dict[str, str] = {}
        for name, value in answer.headers.multi_items():
            key = name.lower()
            found[key] = f"{found[key]}, {value}" if key in found else value
        return EgressResponse(status=answer.status_code, headers=found, content=data, url=str(url))

    async def _read(self, answer: httpx.Response, host: str) -> bytes:
        limit = self._policy.max_response_bytes
        chunks: list[bytes] = []
        size = 0
        async for chunk in answer.aiter_bytes():
            size += len(chunk)
            if size > limit:
                raise ZonicError(DENIED, reason="maxResponseBytes", host=host, limit=limit)
            chunks.append(chunk)
        return b"".join(chunks)
