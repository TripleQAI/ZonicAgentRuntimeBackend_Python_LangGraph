"""Node input and output specifications (design Section 8.2).

Every node type declares its input ports (each with a schema and a binding) and
its output (a payload schema, a declarative map from the kind's raw result, and
aliases). The manifest carries one ``NodeIoSpec`` per node type.
"""

from __future__ import annotations

from typing import Any

from pydantic import Field, model_validator

from zonic_runtime.models.base import ZonicResourceModel


class SchemaRef(ZonicResourceModel):
    """A payload schema by registry ID (``zonic.text@1``) or an inline JSON Schema."""

    ref: str | None = None
    inline: dict[str, Any] | None = None

    @model_validator(mode="after")
    def _exactly_one(self) -> SchemaRef:
        if (self.ref is None) == (self.inline is None):
            raise ValueError("a schema reference needs exactly one of 'ref' or 'inline'")
        return self


class PortBinding(ZonicResourceModel):
    """Sources of a port's value; the first that yields a value wins (I-03)."""

    config_path: str | None = None  # a template held in the node, e.g. 'config.userQueryMapping'
    template: str | None = None  # the type's default, e.g. '{{system.userQuery}}'
    state_path: str | None = None  # e.g. 'history'

    @model_validator(mode="after")
    def _at_least_one(self) -> PortBinding:
        if self.config_path is None and self.template is None and self.state_path is None:
            raise ValueError("a port binding needs a configPath, a template or a statePath")
        return self


class PortSpec(ZonicResourceModel):
    name: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_]*$")
    schema_ref: SchemaRef = Field(alias="schema")
    required: bool = True
    binding: PortBinding
    default: Any = None


class OutputSpec(ZonicResourceModel):
    schema_ref: SchemaRef = Field(alias="schema")
    map: dict[str, str] = Field(default_factory=dict)  # payload field <- path into the raw result
    aliases: dict[str, str] = Field(default_factory=dict)  # extra names for template paths
    node_schema_extends: bool = True  # the node's own outputSchema refines the base
    write_to: str | None = None  # also write the output to this state path


class NodeIoSpec(ZonicResourceModel):
    inputs: list[PortSpec] = Field(default_factory=list)
    output: OutputSpec | None = None
