"""Gateway relation metadata on row and argument models (design Section 15.1, lever L9).

A row model names the view it reads; an argument model names the procedure it
calls, the number of trailing INOUT placeholders, and whether the call may be
retried. The generic repository reads this metadata, so no module builds a
gateway payload by hand and positional argument order is the model's field order.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TypeVar

from pydantic import BaseModel

M = TypeVar("M", bound=type[BaseModel])

_VIEW_ATTR = "__gateway_view__"
_PROCEDURE_ATTR = "__gateway_procedure__"


@dataclass(frozen=True, slots=True)
class ViewMeta:
    schema_key: str  # key into settings.gateway.schemas, never a literal schema name
    name: str
    owner_column: str | None = None


@dataclass(frozen=True, slots=True)
class ProcedureMeta:
    schema_key: str
    name: str
    placeholders: int = 0  # trailing INOUT parameters, sent as NULL
    idempotent: bool = False  # only idempotent procedures may be retried


def gateway_view(*, schema: str, name: str, owner_column: str | None = None) -> Callable[[M], M]:
    def apply(cls: M) -> M:
        setattr(cls, _VIEW_ATTR, ViewMeta(schema, name, owner_column))
        return cls

    return apply


def gateway_procedure(
    *, schema: str, name: str, placeholders: int = 0, idempotent: bool = False
) -> Callable[[M], M]:
    def apply(cls: M) -> M:
        setattr(cls, _PROCEDURE_ATTR, ProcedureMeta(schema, name, placeholders, idempotent))
        return cls

    return apply


def view_meta(model: type[BaseModel]) -> ViewMeta:
    meta = getattr(model, _VIEW_ATTR, None)
    if not isinstance(meta, ViewMeta):
        raise TypeError(f"{model.__name__} has no @gateway_view metadata")
    return meta


def procedure_meta(model: type[BaseModel]) -> ProcedureMeta:
    meta = getattr(model, _PROCEDURE_ATTR, None)
    if not isinstance(meta, ProcedureMeta):
        raise TypeError(f"{model.__name__} has no @gateway_procedure metadata")
    return meta


def has_procedure_meta(model: type[BaseModel]) -> bool:
    return isinstance(getattr(model, _PROCEDURE_ATTR, None), ProcedureMeta)
