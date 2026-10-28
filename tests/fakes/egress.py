"""Egress fakes: a fixed resolver and an ``httpx`` transport that answers by host and path.

``FakeUpstream`` sees each request as the guard sent it (the pinned address in the URL, the
host in the ``Host`` header) and records it; an unrouted request fails the test. ``FakeEgress``
is an ``EgressPort`` that replays the HTTP recordings (P3-12).
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx

from tests.fakes.recordings import Recordings
from zonic_runtime.errors import ZonicError
from zonic_runtime.integrations.egress.guard import EgressGuard
from zonic_runtime.models.resources.catalogs import EgressPolicy
from zonic_runtime.ports.egress import EgressRequest, EgressResponse

#: ``request -> response``; may be async.
Answer = Callable[[httpx.Request], Any]
PUBLIC = "93.184.216.34"


def policy(**changes: Any) -> EgressPolicy:
    base = {
        "schemes": ["https"],
        "hosts": ["api.example.test", "*.example.org"],
        "ports": [443],
        "block_private_ranges": True,
        "follow_redirects": False,
        "max_response_bytes": 1_000_000,
        "timeout_seconds": 5.0,
    }
    return EgressPolicy.model_validate({**base, **changes})


def resolver(addresses: dict[str, list[str]]) -> Any:
    async def resolve(host: str, port: int) -> list[str]:
        if host not in addresses:
            raise OSError(f"unknown host {host}")
        return addresses[host]

    return resolve


@dataclass
class FakeUpstream:
    routes: dict[tuple[str, str], Answer] = field(default_factory=dict)
    seen: list[httpx.Request] = field(default_factory=list)

    def route(self, host: str, path: str, answer: Answer) -> None:
        self.routes[(host, path)] = answer

    async def handle(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(request)
        key = (request.headers["host"].split(":")[0], request.url.raw_path.split(b"?")[0].decode("ascii"))
        answer = self.routes.get(key)
        if answer is None:
            raise AssertionError(f"unrouted request {request.method} {key}")
        found = answer(request)
        if asyncio.iscoroutine(found):
            found = await found
        return found

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def guard(self, addresses: dict[str, list[str]] | None = None, **changes: Any) -> EgressGuard:
        found = addresses if addresses is not None else {"api.example.test": [PUBLIC]}
        return EgressGuard(policy(**changes), resolver=resolver(found), transport=self.transport())


def http_parameters(request: EgressRequest) -> dict[str, Any]:
    """The recording key of an HTTP call: method, URL and body (P3-12, the Node.js ``httpParameters``)."""
    found: dict[str, Any] = {"method": request.method.upper(), "url": request.url}
    if request.body not in (None, ""):
        found["body"] = request.body
    return found


@dataclass
class FakeEgress:
    """Outbound HTTP in memory, after the Node.js ``FakeEgress``: the allow list of ``policy``
    decides what may be called (no DNS), then ``answer`` or the HTTP recordings reply. An
    unanswered call fails the test."""

    policy: EgressPolicy
    answer: Callable[[EgressRequest], EgressResponse] | None = None
    recordings: Recordings | None = None
    calls: list[EgressRequest] = field(default_factory=list)

    def __post_init__(self) -> None:
        self._guard = EgressGuard(self.policy, resolver=resolver({}))

    @classmethod
    def from_recordings(cls, policy: EgressPolicy, *workflows: str) -> FakeEgress:
        return cls(policy, recordings=Recordings.of(*workflows))

    def allows(self, url: str) -> bool:
        return self._guard.allows(url)

    async def send(self, request: EgressRequest) -> EgressResponse:
        if not self.allows(request.url):
            raise ZonicError("EGRESS_DENIED", reason="host", url=request.url)
        self.calls.append(request)
        if self.answer is not None:
            return self.answer(request)
        recorded = (
            self.recordings.find("http", http_parameters(request)) if self.recordings is not None else None
        )
        if recorded is None or "http" not in recorded["response"]:
            raise AssertionError(f"unrecorded HTTP call: {request.method} {request.url}")
        http = recorded["response"]["http"]
        return EgressResponse(
            status=http["status"],
            headers=dict(http.get("headers") or {}),
            content=str(http.get("body") or "").encode("utf-8"),
            url=request.url,
        )

    async def aclose(self) -> None:
        await self._guard.aclose()
