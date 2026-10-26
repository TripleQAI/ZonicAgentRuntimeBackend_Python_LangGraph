"""``DocumentNormalizer``: either dialect onto the canonical models (design 5.1 step 4, 5.2; plan P1-03).

Driven entirely by ``resources/dialects.json``: the code holds generic operations and names
no dialect, node type or kind (design rule 1). The stored document is never changed. Every
rule that fires becomes a blueprint finding (``DIALECT_RULE``, severity ``info``, with the
rule's ID and class), and every field no rule or model covers is reported
(``DOCUMENT_UNKNOWN_FIELD``) and dropped from the canonical copy.
"""

from __future__ import annotations

import copy
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from zonic_runtime.models.blueprint import BlueprintWarning
from zonic_runtime.models.document import WorkflowDocument, WorkflowEdge, WorkflowNode
from zonic_runtime.models.resources.misc import DialectMatch, DialectRule, DialectRules
from zonic_runtime.workflow.loader.dialect_operations import (
    OPERATIONS,
    Firing,
    OperationContext,
    RuleExpressionMode,
)
from zonic_runtime.workflow.loader.paths import JsonObject, delete_path, get_path, has_path, type_at

#: Finding codes the normalizer itself writes.
RULE_FIRED = "DIALECT_RULE"
UNKNOWN_FIELD = "DOCUMENT_UNKNOWN_FIELD"
_NOTE_TOKEN = re.compile(r"\{\{\s*([\w.]+)\s*\}\}")


@dataclass(frozen=True)
class NormalizedDocument:
    document: WorkflowDocument
    findings: list[BlueprintWarning]

    @property
    def fired_rules(self) -> list[str]:
        return [f.details["rule_id"] for f in self.findings if "rule_id" in f.details]

    @property
    def errors(self) -> list[BlueprintWarning]:
        return [f for f in self.findings if f.severity == "error"]


def _model_fields(model: type[Any]) -> set[str]:
    return {info.alias or name for name, info in model.model_fields.items()}


_DOCUMENT_FIELDS = _model_fields(WorkflowDocument)
_NODE_FIELDS = _model_fields(WorkflowNode)
_EDGE_FIELDS = _model_fields(WorkflowEdge)


def _matches(item: JsonObject, when: DialectMatch | None) -> bool:
    if when is None:
        return True
    if when.has is not None and not has_path(item, when.has):
        return False
    if when.non_empty is not None:
        value = get_path(item, when.non_empty)
        if not isinstance(value, list) or not value:
            return False
    if any(get_path(item, path) != expected for path, expected in when.equals.items()):
        return False
    return all(type_at(item, path) == expected for path, expected in when.type_of.items())


def _render(template: str, firing: Firing, item: JsonObject) -> str:
    def value(match: re.Match[str]) -> str:
        found = firing.values.get(match.group(1), get_path(item, match.group(1)))
        return "" if found is None else found if isinstance(found, str) else str(found)

    return _NOTE_TOKEN.sub(value, template)


class DocumentNormalizer:
    """``kind_of`` maps a node type to its manifest kind, for rules that name kinds."""

    def __init__(
        self,
        dialects: DialectRules,
        kind_of: Callable[[str], str | None],
        *,
        default_model: str | None = None,
        rule_expression: RuleExpressionMode = "unsupported",
    ) -> None:
        unknown = sorted({r.op for r in dialects.rules} - set(OPERATIONS))
        if unknown:
            raise ValueError(f"dialect rules use unknown operations: {unknown}")
        self._dialects = dialects
        self._kind_of = kind_of
        self._default_model = default_model
        self._rule_expression = rule_expression
        self._classes = {r.id: r.rule_class for r in dialects.rules}

    def rule_class(self, rule_id: str) -> str | None:
        return self._classes.get(rule_id)

    def normalize(self, stored: JsonObject) -> NormalizedDocument:
        document = copy.deepcopy(stored)
        nodes = [n for n in document.get("nodes") or [] if isinstance(n, dict)]
        edges = [e for e in document.get("edges") or [] if isinstance(e, dict)]
        for node in nodes:
            for key in self._dialects.layout_keys:
                delete_path(node, key)
        for edge in edges:
            for key in self._dialects.edge_layout_keys:
                delete_path(edge, key)

        context = OperationContext(document, self._dialects, self._default_model, self._rule_expression)
        findings: list[BlueprintWarning] = []
        for rule in self._dialects.rules:
            if rule.target == "document":
                self._apply(rule, document, "", context, findings, None)
            elif rule.target == "node":
                for node in nodes:
                    if self._applies(rule, str(node.get("type"))):
                        self._apply(
                            rule, node, f"nodes[{node.get('id')}]", context, findings, str(node.get("id"))
                        )
            else:
                for edge in edges:
                    self._apply(rule, edge, f"edges[{edge.get('id', '')}]", context, findings, None)

        self._drop(document, _DOCUMENT_FIELDS, self._dialects.ignored_document_fields, "", findings, None)
        for node in nodes:
            ignored = self._dialects.ignored_config_fields.get(str(node.get("type")), [])
            config = node.get("config")
            if isinstance(config, dict):
                for key in ignored:
                    config.pop(key, None)
            self._drop(node, _NODE_FIELDS, [], f"nodes[{node.get('id')}]", findings, str(node.get("id")))
        for edge in edges:
            self._drop(edge, _EDGE_FIELDS, [], f"edges[{edge.get('id', '')}]", findings, None)
        return NormalizedDocument(WorkflowDocument.model_validate(document), findings)

    def _applies(self, rule: DialectRule, node_type: str) -> bool:
        if not rule.node_types and not rule.kinds:
            return True
        return node_type in rule.node_types or self._kind_of(node_type) in rule.kinds

    def _apply(
        self,
        rule: DialectRule,
        item: JsonObject,
        path: str,
        context: OperationContext,
        findings: list[BlueprintWarning],
        node_id: str | None,
    ) -> None:
        if not _matches(item, rule.when):
            return
        for firing in OPERATIONS[rule.op](item, rule.args, context):
            findings.append(
                BlueprintWarning(
                    code=firing.code or RULE_FIRED,
                    severity=firing.severity or "info",
                    message=_render(rule.note, firing, item),
                    node_id=node_id,
                    details={"rule_id": rule.id, "class": rule.rule_class, "path": path},
                )
            )

    def _drop(
        self,
        item: JsonObject,
        known: set[str],
        ignored: Iterable[str],
        path: str,
        findings: list[BlueprintWarning],
        node_id: str | None,
    ) -> None:
        silent = set(ignored)
        for key in [k for k in item if k not in known]:
            del item[key]
            if key not in silent:
                findings.append(
                    BlueprintWarning(
                        code=UNKNOWN_FIELD,
                        message=f"unknown field {key} is ignored",
                        node_id=node_id,
                        details={"path": f"{path}.{key}" if path else key},
                    )
                )
