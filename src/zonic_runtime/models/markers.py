"""Field metadata carried by ``Annotated`` (design Section 15.1, lever L4).

``TemplateField`` marks configuration fields whose values are resolved as
templates per node visit; ``SecretRef`` marks fields that hold a secret
reference resolved through the secret port; ``Column`` maps a gateway row or
argument field to its database column. Code finds marked fields through
``marked_fields`` instead of keeping its own lists of field names.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel


@dataclass(frozen=True, slots=True)
class TemplateField:
    """The field's value is a template resolved per node visit."""

    typed: bool = True  # an exact single token keeps its type


@dataclass(frozen=True, slots=True)
class SecretRef:
    """The field holds a secret reference; a literal secret fails validation."""


@dataclass(frozen=True, slots=True)
class Column:
    """Database column backing a gateway row or argument field."""

    name: str


def marked_fields(model: type[BaseModel], marker: type[Any]) -> dict[str, Any]:
    """Field name -> marker instance, for every field annotated with ``marker``."""
    found: dict[str, Any] = {}
    for name, info in model.model_fields.items():
        for meta in info.metadata:
            if isinstance(meta, marker):
                found[name] = meta
    return found


def marked_values(instance: BaseModel, marker: type[Any]) -> dict[str, Any]:
    """Field name -> value, for the fields annotated with ``marker`` that hold a value.

    The template resolver reads only these (case M-08): an unmarked field is never resolved.
    """
    return {
        name: getattr(instance, name)
        for name in marked_fields(type(instance), marker)
        if getattr(instance, name) is not None
    }


def column_map(model: type[BaseModel]) -> dict[str, str]:
    """Field name -> column name; fields without ``Column`` use their own name."""
    columns = marked_fields(model, Column)
    return {name: columns[name].name if name in columns else name for name in model.model_fields}
