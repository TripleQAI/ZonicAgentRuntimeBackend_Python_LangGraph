"""Conditions of Rule nodes, conditional updates and hooks (design 5.3; plan P1-06).

Ported from the Node.js ``conditions.ts`` and ``rule-branches.ts``. The operators are the
dialects' canonical operators (the builder's ``conditionOperators`` plus ``empty``,
``not_empty`` and ``not_contains``); the normalizer has already mapped every alias onto them.
Comparison is type-aware: booleans compare as booleans, numbers as numbers, strings after
trimming, so ``true`` and ``"true"`` are equal (the Django runtime's defect is not carried
forward). ``contains`` works on strings, lists (an equal element) and objects (a key).
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from zonic_runtime.errors import ZonicError
from zonic_runtime.models.nodes import Condition, RuleBranch

Resolve = Callable[[Any], Any]

_BOOLEAN_TEXT = MappingProxyType({"true": True, "false": False})


def _boolean(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return _BOOLEAN_TEXT.get(value.strip().lower())
    return None


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value) if math.isfinite(value) else None
    if isinstance(value, str) and value.strip():
        try:
            parsed = float(value.strip())
        except ValueError:
            return None
        return parsed if math.isfinite(parsed) else None
    return None


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def is_empty(value: Any) -> bool:
    """Missing, ``None``, a blank string, an empty list or an empty object."""
    return (
        value is None
        or (isinstance(value, str) and not value.strip())
        or (isinstance(value, list | dict) and not value)
    )


def loosely_equal(left: Any, right: Any) -> bool:
    if isinstance(left, bool) or isinstance(right, bool):
        a, b = _boolean(left), _boolean(right)
        if a is not None and b is not None:
            return a == b
    if _is_number(left) or _is_number(right):
        x, y = _number(left), _number(right)
        if x is not None and y is not None:
            return x == y
    return _text(left) == _text(right)


def _is_number(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def _order(left: Any, right: Any) -> float | None:
    x, y = _number(left), _number(right)
    if x is not None and y is not None:
        return x - y
    if isinstance(left, str) and isinstance(right, str):
        a, b = left.strip(), right.strip()
        return float((a > b) - (a < b))
    return None


def _contains(container: Any, item: Any) -> bool:
    if isinstance(container, list):
        return any(loosely_equal(element, item) for element in container)
    if isinstance(container, dict):
        return isinstance(item, str) and item in container
    if container is None:
        return False
    return _text(item) in _text(container)


def _ordered(test: Callable[[float], bool]) -> Callable[[Any, Any], bool]:
    def compare(left: Any, right: Any) -> bool:
        found = _order(left, right)
        return found is not None and test(found)

    return compare


COMPARISONS: Mapping[str, Callable[[Any, Any], bool]] = MappingProxyType(
    {
        "eq": loosely_equal,
        "ne": lambda left, right: not loosely_equal(left, right),
        "gt": _ordered(lambda d: d > 0),
        "gte": _ordered(lambda d: d >= 0),
        "lt": _ordered(lambda d: d < 0),
        "lte": _ordered(lambda d: d <= 0),
        "contains": _contains,
        "not_contains": lambda left, right: not _contains(left, right),
        "exists": lambda left, _: left is not None,
        "not_exists": lambda left, _: left is None,
        "empty": lambda left, _: is_empty(left),
        "not_empty": lambda left, _: not is_empty(left),
    }
)


class ConditionEvaluator:
    """``operators``: the dialects' canonical operators; each must have an implementation."""

    def __init__(self, operators: Collection[str]) -> None:
        missing = sorted(set(operators) - set(COMPARISONS))
        if missing:
            raise ValueError(f"condition operators without an implementation: {missing}")
        self._allowed = frozenset(operators)

    def test(self, condition: Condition, resolve: Resolve) -> bool:
        op = condition.op or condition.operator or ""
        if op not in self._allowed:
            raise ZonicError("PLAN_INVALID", reason=f"unknown condition operator {op!r}")
        return COMPARISONS[op](resolve(condition.field), resolve(condition.value))

    def all(self, conditions: Sequence[Condition], logic: str | None, resolve: Resolve) -> bool:
        """Joined by ``AND`` (default) or ``OR``; an empty list is true for AND, false for OR."""
        if (logic or "AND").upper() == "OR":
            return any(self.test(c, resolve) for c in conditions)
        return all(self.test(c, resolve) for c in conditions)


@dataclass(frozen=True)
class BranchChoice:
    #: The handle to activate; ``None`` when nothing matched and there is no default.
    handle: str | None = None
    #: The rule whose conditions matched, when one did.
    matched: str | None = None


def _enabled(branch: RuleBranch) -> bool:
    return branch.enabled is not False and branch.enable is not False


def select_branch(
    branches: Sequence[RuleBranch],
    default_rule_ids: Sequence[str],
    evaluator: ConditionEvaluator,
    resolve: Resolve,
    outgoing: Collection[str] | None = None,
) -> BranchChoice:
    """The first enabled branch whose conditions hold (a branch without conditions never
    matches unless it is the default, as in the builder); else the branch marked
    ``isDefault``; else the first of ``defaultRuleIds`` the node has an edge for."""
    for branch in branches:
        if not _enabled(branch) or branch.is_default or not branch.conditions:
            continue
        if evaluator.all(branch.conditions, branch.condition_logic or branch.logic_type, resolve):
            return BranchChoice(handle=branch.rule_id, matched=branch.rule_id)
    explicit = next((b for b in branches if _enabled(b) and b.is_default), None)
    if explicit is not None:
        return BranchChoice(handle=explicit.rule_id)
    fallback = next((i for i in default_rule_ids if outgoing is None or i in outgoing), None)
    return BranchChoice(handle=fallback)
