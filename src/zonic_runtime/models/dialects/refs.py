"""Canonical forms the normalizer produces from either dialect (design 5.2, 15.1 "Dialects").

The normalizer writes them into node configurations as plain JSON; consumers validate
the values they read with these models (``ModelRef.model_validate(config.model)``).
"""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import Field

from zonic_runtime.models.base import ZonicModel
from zonic_runtime.models.markers import TemplateField
from zonic_runtime.models.nodes.base import ConditionGroup


class ModelRef(ZonicModel):
    """A model by catalog code or provider model ID, with call parameters."""

    model_id: str = Field(min_length=1)
    provider: str | None = None
    vendor: str | None = None
    name: str | None = None
    temperature: float | None = None
    top_p: float | None = None
    max_tokens: int | None = None


class ToolRef(ZonicModel):
    """A tool an agent or LLM node may call itself."""

    tool_type: str
    name: str
    id: str | None = None
    title: str | None = None
    description: str | None = None
    tool_id: str | None = None
    server_id: str | None = None
    schema_: dict[str, Any] | None = Field(default=None, alias="schema")
    enabled: bool = True
    settings: dict[str, Any] = Field(default_factory=dict)


class RoutingToolRef(ZonicModel):
    """A platform routing tool: calling it follows the handoff edge to ``target``.

    ``parameters`` are preset arguments (templates) merged over what the model supplies.
    """

    id: str
    name: str
    target: str
    alias: str | None = None
    title: str | None = None
    description: str | None = None
    schema_: dict[str, Any] | None = Field(default=None, alias="schema")
    parameters: Annotated[dict[str, Any] | None, TemplateField()] = None
    enabled: bool = True
    return_direct: bool = False

    @property
    def call_name(self) -> str:
        return self.alias or self.name


class OutputMappingEntry(ZonicModel):
    """One output field: ``target`` receives the resolved ``value`` template."""

    target: str
    value: Any = None
    type: str | None = None


__all__ = ["ConditionGroup", "ModelRef", "OutputMappingEntry", "RoutingToolRef", "ToolRef"]
