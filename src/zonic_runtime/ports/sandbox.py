"""The sandbox port: Script node code runs in a sandbox service, never in the runtime's process
(design 7.5; plan P3-07). A failing, killed or unparsable script is ``SANDBOX_ERROR``."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True)
class SandboxRequest:
    language: str
    code: str
    #: The node's resolved inputs; the script reads them as ``input``.
    input: Mapping[str, Any] = field(default_factory=dict)
    timeout_seconds: float = 3.0


@dataclass(frozen=True)
class SandboxResult:
    #: What the script returned.
    result: Any
    stdout: str = ""
    stderr: str = ""


@runtime_checkable
class SandboxPort(Protocol):
    async def run(self, request: SandboxRequest) -> SandboxResult: ...

    async def aclose(self) -> None: ...
