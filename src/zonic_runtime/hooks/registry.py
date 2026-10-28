"""Hook types by name (design 13, "Adding a hook type")."""

from __future__ import annotations

from collections.abc import Iterable

from zonic_runtime.hooks.types import NodeHook


class HookRegistry:
    def __init__(self, types: Iterable[NodeHook] = ()) -> None:
        self._types: dict[str, NodeHook] = {}
        for hook in types:
            self.register(hook)

    def register(self, hook: NodeHook) -> HookRegistry:
        if hook.name in self._types:
            raise ValueError(f"hook type {hook.name} is registered twice")
        self._types[hook.name] = hook
        return self

    def get(self, name: str) -> NodeHook | None:
        return self._types.get(name)

    def names(self) -> list[str]:
        return sorted(self._types)
