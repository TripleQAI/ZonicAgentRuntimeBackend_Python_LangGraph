"""The builder's blueprint rules (``validation_rules.json`` entries with an ID: SW-1 to SW-12).

Ported from the Node.js runtime's ``validation-rules.ts``. Rules are evaluated with the
builder's own vocabulary and wording, so the runtime rejects exactly what the builder's
validator rejects (case D-11). Paths are dot paths from the node root.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from zonic_runtime.models.blueprint import BlueprintWarning
from zonic_runtime.models.resources.builder import BuilderRegistry, BuilderRule, RuleCheck
from zonic_runtime.runtime.paths import JsonObject, get_path

#: Finding code of a builder rule; ``details.rule_id`` names the rule.
BUILDER_RULE = "BUILDER_RULE"
_COUNT = re.compile(r"^count\((.+)\)$")
_TOKEN = re.compile(r"\{(\w+)\}")


@dataclass(frozen=True)
class RuleNode:
    id: str
    type: str
    name: str
    raw: JsonObject

    @property
    def label(self) -> str:
        return self.name or self.id


@dataclass(frozen=True)
class RuleEdge:
    id: str
    source: str
    target: str


def _refs(node: RuleNode, path: str) -> list[str]:
    value = get_path(node.raw, path)
    return [v for v in value if isinstance(v, str)] if isinstance(value, list) else []


def _render(message: str, node: RuleNode, values: Mapping[str, Any]) -> str:
    def token(match: re.Match[str]) -> str:
        key = match.group(1)
        if key == "label":
            return node.label
        if key == "type":
            return node.type
        value = values.get(key)
        return "" if value is None else str(value)

    return _TOKEN.sub(token, message)


def _blank(value: Any) -> bool:
    return value is None or value == ""


class BuilderRuleEvaluator:
    def __init__(self, registry: BuilderRegistry) -> None:
        self._registry = registry

    def evaluate(self, nodes: Sequence[RuleNode], edges: Sequence[RuleEdge]) -> list[BlueprintWarning]:
        by_id = {n.id: n for n in nodes}
        findings: list[BlueprintWarning] = []
        for node in nodes:
            for rule in self._registry.validation_rules.get(node.type, []):
                for values in self._check(rule, rule.rule, rule.path or "", node, nodes, by_id, edges):
                    message = _render(rule.message, node, values)
                    findings.append(
                        BlueprintWarning(
                            code=BUILDER_RULE,
                            severity=rule.severity,
                            message=message,
                            node_id=node.id,
                            details={
                                "rule_id": rule.id,
                                "path": f"nodes[{node.id}].{rule.path or ''}".rstrip("."),
                            },
                        )
                    )
        return findings

    def _check(
        self,
        rule: BuilderRule,
        kind: str,
        path: str,
        node: RuleNode,
        nodes: Sequence[RuleNode],
        by_id: Mapping[str, RuleNode],
        edges: Sequence[RuleEdge],
    ) -> list[dict[str, Any]]:
        """One entry per violation, with the values its message names."""

        def ref_label(ref: str) -> str:
            return by_id[ref].label if ref in by_id else ref

        match kind:
            case "non_empty_list":
                value = get_path(node.raw, path)
                return [] if isinstance(value, list) and value else [{}]
            case "required":
                return [{}] if _blank(get_path(node.raw, path)) else []
            case "forbidden":
                value = get_path(node.raw, path)
                return [] if _blank(value) or value == [] else [{}]
            case "refs_exist":
                allowed = (
                    self._allowed_types(rule.allowed_types_from, node.type)
                    if rule.allowed_types_from
                    else None
                )
                return [
                    {"ref": ref, "refLabel": ref_label(ref)}
                    for ref in _refs(node, path)
                    if ref not in by_id or (allowed is not None and by_id[ref].type not in allowed)
                ]
            case "unique_by":
                counts: dict[str, int] = {}
                for ref in _refs(node, path):
                    target = by_id.get(ref)
                    value = (
                        ""
                        if target is None
                        else (
                            target.name
                            if rule.field == "name"
                            else str(get_path(target.raw, rule.field or "") or "")
                        )
                    )
                    counts[value] = counts.get(value, 0) + 1
                return [{"value": v} for v, n in counts.items() if v and n > 1]
            case "one_of_refs":
                value = get_path(node.raw, path)
                if _blank(value):
                    return []
                return [] if str(value) in _refs(node, rule.refs_path or "") else [{"value": value}]
            case "exclusive_refs":
                # A shared member is reported once, on the first container that lists it.
                position = {n.id: i for i, n in enumerate(nodes)}
                others = [o for o in nodes if o is not node and o.type == node.type]
                earlier = [o for o in others if position[o.id] < position[node.id]]
                return [
                    {"ref": ref, "refLabel": ref_label(ref)}
                    for ref in _refs(node, path)
                    if any(ref in _refs(o, path) for o in others)
                    and not any(ref in _refs(o, path) for o in earlier)
                ]
            case "no_edges_to_refs":
                mine = set(_refs(node, path))
                found = []
                for edge in edges:
                    if edge.source in mine or edge.target in mine:
                        ref = edge.target if edge.target in mine else edge.source
                        found.append({"edgeId": edge.id, "ref": ref, "refLabel": ref_label(ref)})
                return found
            case "each_ref":
                nested: RuleCheck | None = rule.check
                if nested is None:
                    return []
                return [
                    {"ref": target.id, "refLabel": target.label}
                    for ref in _refs(node, path)
                    if (target := by_id.get(ref)) is not None
                    and self._check(rule, nested.rule, nested.path or "", target, nodes, by_id, edges)
                ]
            case "compare":
                if rule.when and not self._compare(node, rule.when.left, rule.when.op, rule.when.right):
                    return []
                lefts = rule.left if isinstance(rule.left, list) else [rule.left]
                return (
                    [{}]
                    if any(self._compare(node, left, rule.op or "", rule.right) is False for left in lefts)
                    else []
                )
        return []

    def _allowed_types(self, source: str, node_type: str) -> list[str]:
        group, _, key = source.partition(".")
        semantics = self._registry.semantics.model_dump(by_alias=True)
        value = (semantics.get(group) or {}).get(node_type, {}).get(key)
        return [v for v in value if isinstance(v, str)] if isinstance(value, list) else []

    @staticmethod
    def _operand(node: RuleNode, operand: Any) -> Any:
        """``count(path)``, a dot path into the node, or a literal."""
        if not isinstance(operand, str):
            return operand
        if (count := _COUNT.match(operand)) is not None:
            value = get_path(node.raw, count.group(1))
            return len(value) if isinstance(value, list) else 0
        return get_path(node.raw, operand)

    def _compare(self, node: RuleNode, left_operand: Any, op: str, right_operand: Any) -> bool | None:
        """``None`` when an operand is missing or not a number: the builder skips the check then."""
        left = self._operand(node, left_operand)
        if not _number(left):
            return None
        if op == "between":
            bounds = (
                [self._operand(node, b) for b in right_operand] if isinstance(right_operand, list) else []
            )
            if len(bounds) == 2 and _number(bounds[0]) and _number(bounds[1]):
                return bool(bounds[0] <= left <= bounds[1])
            return None
        right = self._operand(node, right_operand)
        if not _number(right):
            return None
        match op:
            case ">=":
                return bool(left >= right)
            case ">":
                return bool(left > right)
            case "<=":
                return bool(left <= right)
            case "<":
                return bool(left < right)
            case "==":
                return bool(left == right)
        return None


def _number(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)
