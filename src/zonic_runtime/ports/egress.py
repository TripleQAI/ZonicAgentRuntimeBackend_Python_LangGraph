"""The egress port: every outbound HTTP call of a workflow (design 15, "Egress"; plan P3-03).

Kinds call ``EgressPort.send``; the adapter (``integrations/egress``) applies the allow list,
blocks private addresses after resolving the host, never follows a redirect it has not checked,
caps the response size and the time. Tests replace it with a fake.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True)
class EgressRequest:
    method: str
    url: str
    #: In order; a name may repeat.
    headers: tuple[tuple[str, str], ...] = ()
    #: Text is sent as is; any other value as JSON.
    body: Any = None
    timeout_seconds: float | None = None


@dataclass(frozen=True)
class EgressResponse:
    status: int
    #: Lower-case names; repeated headers joined with ", ".
    headers: dict[str, str] = field(default_factory=dict)
    content: bytes = b""
    #: The URL that answered (after the redirects the policy allowed).
    url: str = ""

    def text(self) -> str:
        charset = "utf-8"
        for part in self.headers.get("content-type", "").split(";")[1:]:
            name, _, value = part.strip().partition("=")
            if name.lower() == "charset" and value:
                charset = value.strip('"')
        try:
            return self.content.decode(charset, errors="replace")
        except LookupError:
            return self.content.decode("utf-8", errors="replace")


@runtime_checkable
class EgressPort(Protocol):
    async def send(self, request: EgressRequest) -> EgressResponse:
        """The response, whatever its status; raises ``ZonicError`` with ``EGRESS_DENIED``,
        ``TIMEOUT`` or ``UPSTREAM_5XX`` (a 5xx answer, or no answer at all)."""
        ...

    def allows(self, url: str) -> bool:
        """Whether the allow list admits ``url``, without sending anything."""
        ...

    async def aclose(self) -> None: ...
