"""Validation problems with field paths, for start-up failures and 400 responses."""

from __future__ import annotations

from pydantic import ValidationError

from .base import ZonicModel


class ValidationProblem(ZonicModel):
    path: str
    message: str
    type: str


def problems_from(exc: ValidationError, *, prefix: str = "") -> list[ValidationProblem]:
    """Flatten a Pydantic error into dotted paths (list indexes in brackets)."""
    problems: list[ValidationProblem] = []
    for err in exc.errors(include_url=False):
        parts: list[str] = [prefix] if prefix else []
        for loc in err["loc"]:
            if isinstance(loc, int) and parts:
                parts[-1] = f"{parts[-1]}[{loc}]"
            else:
                parts.append(str(loc))
        problems.append(ValidationProblem(path=".".join(parts), message=err["msg"], type=err["type"]))
    return problems
