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
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, Literal

from zonic_runtime.errors import ZonicError
from zonic_runtime.models.blueprint import Blueprint
from zonic_runtime.models.resources.settings import RuntimeSettings
from zonic_runtime.ports.datasets import RunDatasets
from zonic_runtime.ports.mcp import RunMcp
from zonic_runtime.ports.rows import RowReader
from zonic_runtime.ports.secrets import SecretPort

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
    #: The MCP port bound to the run (P3-01); tools read it through ``ToolRuntime.context``.
    mcp: RunMcp | None = None
    egress: Any = None  # P3-04
    sandbox: Any = None  # P3-07
    secrets: SecretPort | None = None
    #: The dataset port bound to the run and its pending datasets (P3-04, P3-05).
    datasets: RunDatasets | None = None


class RunPermits:
    """The run's permits (design 11.2). Nodes: one pool shared by nested graphs, so a run never
    executes more than ``engine.maxConcurrencyPerRun`` leaf nodes at once; container kinds hold
    none while their children run, so nesting cannot deadlock. Tools: ``tools.maxConcurrentPerTurn``
    per node, so one agent turn's tool calls are bounded (case Q-07)."""

    def __init__(self, nodes: int, tools: int = 4) -> None:
        self._nodes = asyncio.Semaphore(nodes)
        self.size = nodes
        self._tool_size = tools
        self._tools: dict[str, asyncio.Semaphore] = {}
        #: The most leaf nodes and tool calls in flight at once, for tests (cases Q-06, Q-07).
        self.peak_nodes = 0
        self.peak_tools = 0

    @asynccontextmanager
    async def node(self) -> AsyncIterator[None]:
        async with self._nodes:
            self.peak_nodes = max(self.peak_nodes, self.size - self._nodes._value)
            yield

    @asynccontextmanager
    async def tool(self, node_id: str) -> AsyncIterator[None]:
        permits = self._tools.setdefault(node_id, asyncio.Semaphore(self._tool_size))
        async with permits:
            self.peak_tools = max(self.peak_tools, self._tool_size - permits._value)
            yield

    @property
    def available(self) -> int:
        return self._nodes._value  # asyncio exposes no public reading

    @property
    def in_use(self) -> int:
        return self.size - self.available


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


class ToolCallKeys:
    """``nodeId:visit:callIndex`` per tool call (DR-2): ``callIndex`` counts a visit's tool calls
    in issue order, so a re-executed visit (a retry, a resumed turn) whose model asks again in the
    same order sends the same keys, though the model's call IDs differ. Per run, never shared."""

    def __init__(self) -> None:
        self._next: dict[tuple[str, int], int] = {}
        self._keys: dict[str, str] = {}

    def start(self, node_id: str, visit: int) -> None:
        """A visit (or an attempt of it) begins: its calls count from 0 again."""
        self._next[(node_id, visit)] = 0

    def assign(self, node_id: str, visit: int, call_id: str) -> str:
        found = self._keys.get(call_id)
        if found is None:
            index = self._next.get((node_id, visit), 0)
            self._next[(node_id, visit)] = index + 1
            found = self._keys[call_id] = f"{node_id}:{visit}:{index}"
        return found

    def key(self, call_id: str) -> str | None:
        return self._keys.get(call_id)


class ChildBlueprints:
    """The child blueprints a run's sub-flows opened. Per run (in ``RunContext``), never shared."""

    def __init__(self) -> None:
        self._by_node: dict[tuple[str, str], Blueprint] = {}

    def add(self, parent: Blueprint, node_id: str, child: Blueprint) -> None:
        self._by_node[(parent.workflow_id, node_id)] = child

    def get(self, parent: Blueprint, node_id: str) -> Blueprint | None:
        return self._by_node.get((parent.workflow_id, node_id))


class RunDocuments:
    """The workflow documents a run compiled, as stored, by workflow ID: the root and the children
    its sub-flows opened. A pause saves them; a resumed run's are pinned, so its sub-flows open
    the children it paused with (design 3.5, case H-09). Per run, never shared."""

    def __init__(self, pinned: Mapping[str, Any] | None = None) -> None:
        self._stored: dict[str, Any] = dict(pinned or {})
        self._pinned = frozenset(self._stored)

    def add(self, workflow_id: str, stored: Any) -> None:
        if stored is not None:
            self._stored.setdefault(workflow_id, stored)

    def pinned(self, workflow_id: str) -> Any:
        """The stored document a resumed run must use, or ``None``."""
        return self._stored.get(workflow_id) if workflow_id in self._pinned else None

    def stored(self) -> dict[str, Any]:
        return dict(self._stored)


@dataclass(frozen=True, slots=True)
class RunContext:
    run: RunIdentity
    deadline: Deadline
    blueprint: Blueprint
    settings: RuntimeSettings
    permits: RunPermits
    ports: RunPorts = field(default_factory=RunPorts)
    emitter: RunEmitter = field(default_factory=RunEmitter)
    governor: Any = None  # ModelGovernor, P2-05
    hooks: Any = None  # TemplateHooks, P2-06
    #: The run's ``RunTrace`` (P2-07); ``None`` is tracing off.
    tracer: Any = None
    #: The idempotency keys of the run's tool calls (DR-2; P3-02).
    tool_calls: ToolCallKeys = field(default_factory=lambda: ToolCallKeys())
    #: Child blueprints the run's sub-flow nodes opened, by ``(workflow ID, node ID)`` (P3-09):
    #: the event mapper places a child's events with them. Filled by the sub-flow kind.
    children: ChildBlueprints = field(default_factory=lambda: ChildBlueprints())
    #: The documents the run compiled (P4-02): saved with a pause, pinned on its resume.
    documents: RunDocuments = field(default_factory=lambda: RunDocuments())
