"""Manifest kinds to kind classes (design 8.4; plan P1-10).

The registry is the only place a kind name meets code. A node type gains a kind by a
manifest entry alone (case G-10, I-11); a kind without a class cannot be compiled.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping

from zonic_runtime.engine.kinds.base import NodeKind
from zonic_runtime.errors import ZonicError
from zonic_runtime.models.blueprint import Blueprint, NodeSpec

#: ``(spec, blueprint) -> kind instance``; usually the kind class itself.
KindFactory = Callable[[NodeSpec, Blueprint], NodeKind]


class KindRegistry:
    def __init__(self, kinds: Mapping[str, KindFactory]) -> None:
        self._kinds = dict(kinds)

    def create(self, spec: NodeSpec, blueprint: Blueprint) -> NodeKind:
        factory = self._kinds.get(spec.kind)
        if factory is None:
            raise ZonicError(
                "NODE_TYPE_UNSUPPORTED", node_id=spec.node_id, type=spec.node_type, kind=spec.kind
            )
        return factory(spec, blueprint)

    def kinds(self) -> list[str]:
        return sorted(self._kinds)

    def with_kinds(self, kinds: Mapping[str, KindFactory]) -> KindRegistry:
        return KindRegistry({**self._kinds, **kinds})
