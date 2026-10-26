"""Loading ``resources/*.json`` into their models, with one clear error per problem."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ValidationError

from zonic_runtime.errors import StartupError
from zonic_runtime.models.errors import problems_from


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise StartupError("RESOURCE_MISSING", file=str(path)) from exc
    except json.JSONDecodeError as exc:
        raise StartupError("RESOURCE_INVALID", file=str(path), line=exc.lineno, message=exc.msg) from exc


def validate_resource[M: BaseModel](data: Any, model: type[M], *, source: str) -> M:
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        problems = [p.wire() for p in problems_from(exc)]
        raise StartupError("RESOURCE_INVALID", file=source, problems=problems) from exc


def deep_merge(base: Any, overlay: Any) -> Any:
    """Objects merge key by key; any other overlay value replaces the base value."""
    if isinstance(base, dict) and isinstance(overlay, dict):
        merged = dict(base)
        for key, value in overlay.items():
            merged[key] = deep_merge(base[key], value) if key in base else value
        return merged
    return overlay
