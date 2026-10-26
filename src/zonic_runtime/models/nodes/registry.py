"""Manifest kind -> configuration model, derived from class names (no kind literals in code)."""

from __future__ import annotations

from functools import cache

from zonic_runtime.models.nodes import configs  # noqa: F401  (defines the subclasses)
from zonic_runtime.models.nodes.base import NodeConfigBase, kind_of


@cache
def config_models() -> dict[str, type[NodeConfigBase]]:
    return {kind_of(model): model for model in NodeConfigBase.__subclasses__()}


def config_model_for(kind: str) -> type[NodeConfigBase]:
    try:
        return config_models()[kind]
    except KeyError as exc:
        raise ValueError(f"no configuration model for node kind {kind!r}") from exc
