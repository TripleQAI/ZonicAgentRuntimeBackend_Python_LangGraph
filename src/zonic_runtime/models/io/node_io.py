"""``NodeInput`` and ``NodeOutput``: the values of one node visit (design 8.2; plan P1-07)."""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Any

from pydantic import Field

from zonic_runtime.models.base import ZonicModel
from zonic_runtime.models.engine.records import DatasetRef, Usage


class NodeOutput(ZonicModel):
    """The output envelope of every node; ``data`` is what ``{{nodes.X.output.*}}`` reads,
    validated against the node's effective output schema."""

    data: dict[str, Any] = Field(default_factory=dict)
    #: Content parts for clients (``models/content``), plain.
    parts: list[dict[str, Any]] = Field(default_factory=list)
    refs: list[DatasetRef] = Field(default_factory=list)
    usage: Usage | None = None


class NodeInput:
    """The validated port values of one visit. Kinds read their data only through ``get``;
    every value was checked against its port's schema before ``execute`` (case I-04)."""

    __slots__ = ("_values", "node_id", "visit")

    def __init__(self, node_id: str, visit: int, values: Mapping[str, Any]) -> None:
        self.node_id = node_id
        self.visit = visit
        self._values: Mapping[str, Any] = MappingProxyType(dict(values))

    def get(self, port: str, default: Any = None) -> Any:
        return self._values.get(port, default)

    def has(self, port: str) -> bool:
        return self._values.get(port) is not None

    def all(self) -> Mapping[str, Any]:
        """Every port value, for hooks and the observation input."""
        return self._values

    def with_values(self, values: Mapping[str, Any]) -> NodeInput:
        """A copy with some ports replaced, for ``beforeNode`` hooks that change the input."""
        return NodeInput(self.node_id, self.visit, {**self._values, **values})

    def __repr__(self) -> str:
        return f"NodeInput({self.node_id!r}, visit={self.visit}, ports={sorted(self._values)})"
