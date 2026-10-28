"""``RunContext``: everything one run needs that is not state (design 7.2; plan P1-08).

Passed as ``context=`` at invocation and read by nodes, middleware and tools through
``runtime.context`` (or ``ToolRuntime.context``). Nothing cached across runs holds it
(rule 14). Ports are bound to the run's owner, so no tool reaches another session's data
(case X-06). Ports that arrive with later tasks (MCP, egress, sandbox, secrets, datasets)
are typed by those tasks; until then they are ``None``.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, Literal

from zonic_runtime.errors import ZonicError
from zonic_runtime.models.blueprint import Blueprint
from zonic_runtime.models.resources.settings import RuntimeSettings
from zonic_runtime.ports.rows import RowReader

RunMode = Literal["stream", "sync", "async"]


@dataclass(frozen=True, slots=True)
class RunIdentity:
    run_id: str
    session_id: str
    owner_sub: str
    tenant_id: str | None
    workflow_id: str
    revision: str
    mode: RunMode = "stream"
    attempt: int = 1


@dataclass(frozen=True, slots=True)
class Deadline:
    """The run deadline on a monotonic clock (``RUN_TIMEOUT`` when it passes)."""

    at: float
    monotonic: Callable[[], float] = time.monotonic

    @classmethod
    def after(cls, seconds: float, monotonic: Callable[[], float] = time.monotonic) -> Deadline:
        return cls(monotonic() + seconds, monotonic)

    def remaining(self) -> float:
        return max(0.0, self.at - self.monotonic())

    def expired(self) -> bool:
        return self.remaining() <= 0.0

    def check(self) -> None:
        if self.expired():
            raise ZonicError("RUN_TIMEOUT")


@dataclass(frozen=True, slots=True)
class RunPorts:
    """Owner-bound ports of one run."""

    rows: RowReader | None = None
    mcp: Any = None  # P3-01
    egress: Any = None  # P3-04
    sandbox: Any = None  # P3-07
    secrets: Any = None  # P3-04
    datasets: Any = None  # P2-05


class RunPermits:
    """The run-wide node permit pool (design 11.2): nested graphs share it, so a run never
    executes more than ``engine.maxConcurrencyPerRun`` nodes at once."""

    def __init__(self, nodes: int) -> None:
        self._nodes = asyncio.Semaphore(nodes)
        self.size = nodes

    @asynccontextmanager
    async def node(self) -> AsyncIterator[None]:
        async with self._nodes:
            yield

    @property
    def available(self) -> int:
        return self._nodes._value  # asyncio exposes no public reading


@dataclass
class RunEmitter:
    """Custom stream events of the run; wraps ``runtime.stream_writer`` and never blocks or
    raises into the node (a failing writer drops the event and counts it)."""

    writer: Callable[[dict[str, Any]], Any] | None = None
    dropped: int = 0

    def emit(self, event: str, **data: Any) -> None:
        if self.writer is None:
            return
        try:
            self.writer({"event": event, **data})
        except Exception:  # noqa: BLE001  # principle: allow rule-17-broad-except emitter-never-raises
            self.dropped += 1


@dataclass(frozen=True, slots=True)
class RunContext:
    run: RunIdentity
    deadline: Deadline
    blueprint: Blueprint
    settings: RuntimeSettings
    permits: RunPermits
    ports: RunPorts = field(default_factory=RunPorts)
    emitter: RunEmitter = field(default_factory=RunEmitter)
    governor: Any = None  # ModelGovernor, P2-02
    hooks: Any = None  # HookRunner, P2-06
