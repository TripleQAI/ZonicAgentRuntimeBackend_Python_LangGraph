"""Models whose JSON Schema is published under ``generated/`` (the client contract).

A model joins the published contract with ``@exported(group)``. ``scripts/gen.py``
writes one schema file per exported model; CI fails when a committed file differs
from the model (case M-03).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TypeVar

from pydantic import BaseModel

M = TypeVar("M", bound=type[BaseModel])

_EXPORTED: dict[tuple[str, str], type[BaseModel]] = {}


def exported(group: str) -> Callable[[M], M]:
    def apply(cls: M) -> M:
        key = (group, cls.__name__)
        existing = _EXPORTED.get(key)
        if existing is not None and existing is not cls:
            raise TypeError(f"two exported models named {group}/{cls.__name__}")
        _EXPORTED[key] = cls
        return cls

    return apply


def exported_models() -> dict[tuple[str, str], type[BaseModel]]:
    return dict(sorted(_EXPORTED.items()))
