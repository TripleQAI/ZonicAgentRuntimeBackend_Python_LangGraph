"""The tracing port (design 12; plan P2-07).

A ``RunTracer`` opens one trace per run: ``begin`` returns the run's ``RunTrace``, whose
``callbacks`` go into the run's invocation config and whose ``active()`` context wraps the
run in the driver. Nodes add ``event`` observations (routing decisions, warnings, loop
iterations) under their own node observation. ``NoopTracer`` is tracing off (case L-16): runs
behave the same.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

Level = Literal["DEBUG", "DEFAULT", "WARNING", "ERROR"]


@dataclass(frozen=True)
class TraceInfo:
    run_id: str
    name: str
    session_id: str | None = None
    user_id: str | None = None
    tags: Sequence[str] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)
    input: Any = None
    #: The run's blueprint: node spans are recognised and typed from it.
    blueprint: Any = None
    #: ``(parent blueprint, sub-flow node ID) -> child blueprint`` (``RunContext.children.get``):
    #: a child workflow's node spans are recognised too (P3-09).
    children: Any = None


class RunTrace(Protocol):
    @property
    def callbacks(self) -> list[Any]: ...

    def active(self) -> Any:
        """A context manager: the run's root observation is current inside it."""
        ...

    def event(
        self, config: Any, name: str, level: Level = "DEFAULT", metadata: Mapping[str, Any] | None = None
    ) -> None:
        """An ``event`` under the node observation of ``config`` (the node's runnable config)."""
        ...

    def span(
        self, config: Any, name: str, level: Level = "DEFAULT", metadata: Mapping[str, Any] | None = None
    ) -> None:
        """A finished child ``span`` under the node observation of ``config`` (a hook execution)."""
        ...

    def annotate(
        self,
        config: Any,
        tags: Sequence[str] = (),
        metadata: Mapping[str, Any] | None = None,
        score: Mapping[str, Any] | None = None,
    ) -> None:
        """Tags, metadata and a score on the node observation of ``config``, or on the run's
        observation when ``config`` is ``None`` (the ``annotate`` hook)."""
        ...

    def end(self, status: str, output: Any = None, error_code: str | None = None) -> None: ...


class RunTracer(Protocol):
    def begin(self, info: TraceInfo) -> RunTrace: ...


class NoopTrace:
    @property
    def callbacks(self) -> list[Any]:
        return []

    @contextmanager
    def active(self) -> Iterator[None]:
        yield

    def event(
        self, config: Any, name: str, level: Level = "DEFAULT", metadata: Mapping[str, Any] | None = None
    ) -> None:
        return None

    def span(
        self, config: Any, name: str, level: Level = "DEFAULT", metadata: Mapping[str, Any] | None = None
    ) -> None:
        return None

    def annotate(
        self,
        config: Any,
        tags: Sequence[str] = (),
        metadata: Mapping[str, Any] | None = None,
        score: Mapping[str, Any] | None = None,
    ) -> None:
        return None

    def end(self, status: str, output: Any = None, error_code: str | None = None) -> None:
        return None


class NoopTracer:
    def begin(self, info: TraceInfo) -> RunTrace:
        return NoopTrace()
