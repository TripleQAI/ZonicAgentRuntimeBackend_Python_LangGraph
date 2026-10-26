"""Base of every node configuration model (design 15.1, lever L4; plan P1-02).

Configuration models are document models: unknown keys are kept and reported, because
the builder's round trip must preserve them. Fields whose values are templates carry
``TemplateField``; fields holding credentials carry ``SecretRef`` and accept only a
reference (``secret:<name>`` or ``{{secrets.<name>}}``) or nothing (case M-08, T-04).
The kind a model serves is derived from its class name (``LlmNodeConfig`` serves kind
``llm``), so node kinds are written only in ``node-runtime.json`` (design rule 1).
"""

from __future__ import annotations

import re
from typing import Annotated, Any, ClassVar

from pydantic import Field, model_validator

from zonic_runtime.models.base import ZonicDocumentModel
from zonic_runtime.models.markers import SecretRef, TemplateField, marked_fields

_SECRET_REFERENCE = re.compile(r"^(secret:[A-Za-z0-9_./-]+|\{\{\s*secrets\.[A-Za-z0-9_.-]+\s*\}\})$")
_CLASS_SUFFIX = "NodeConfig"


def is_secret_reference(value: Any) -> bool:
    """Whether ``value`` may sit in a ``SecretRef`` field: a reference, or nothing."""
    return value in (None, "") or (isinstance(value, str) and bool(_SECRET_REFERENCE.match(value.strip())))


def kind_of(model: type[NodeConfigBase]) -> str:
    """``MultiAgentNodeConfig`` -> ``multi_agent``."""
    stem = model.__name__.removesuffix(_CLASS_SUFFIX)
    return re.sub(r"(?<!^)(?=[A-Z])", "_", stem).lower()


class NodeConfigBase(ZonicDocumentModel):
    """Fields every node type may carry: updates, output fields and the output schema."""

    #: Keys the builders write on every node type; never reported as unknown.
    known_extra_keys: ClassVar[frozenset[str]] = frozenset()

    variable_updates: Annotated[list[Any] | None, TemplateField()] = None
    state_updates: Annotated[list[Any] | None, TemplateField()] = None
    output_structure: list[Any] | None = None
    output_schema: list[Any] | dict[str, Any] | None = None

    @model_validator(mode="after")
    def _no_literal_secrets(self) -> NodeConfigBase:
        for name in marked_fields(type(self), SecretRef):
            if not is_secret_reference(getattr(self, name)):
                raise ValueError(
                    f"{name}: a literal secret is not allowed; use secret:<name> or {{{{secrets.<name>}}}}"
                )
        return self


class Condition(ZonicDocumentModel):
    """One comparison of a rule branch or a conditional update."""

    field: Annotated[Any, TemplateField()] = None
    op: str | None = None
    operator: str | None = None
    value: Annotated[Any, TemplateField()] = None
    logic: str | None = None


class ConditionGroup(ZonicDocumentModel):
    """Conditions joined by ``AND`` or ``OR``: an update's ``when``."""

    condition_logic: str = "AND"
    conditions: list[Condition] = Field(default_factory=list)


class RuleBranch(ZonicDocumentModel):
    """One branch of a rule: its handle is ``rule_id``."""

    rule_id: str | None = None
    id: str | None = None
    label: str | None = None
    conditions: list[Condition] = Field(default_factory=list)
    condition_logic: str | None = None
    logic_type: str | None = None
    is_default: bool | None = None
    enable: bool | None = None
    enabled: bool | None = None
    expression: str | None = None
