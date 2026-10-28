"""Models of the resources/*.json files, validated at start-up (case M-01)."""

from __future__ import annotations

from pydantic import BaseModel

from zonic_runtime.models.hooks.binding import HookBindings

from .builder import BuilderRegistry
from .catalogs import EgressPolicy, McpServerCatalog, ModelCatalog
from .manifest import NodeRuntimeManifest
from .misc import DialectRules, ErrorCatalog, ObservabilityConfig, RunStateMachine, TemplateNamespaces
from .settings import RuntimeSettings

#: Resource file -> model. Settings are listed separately because they have overlays.
RESOURCE_FILES: dict[str, type[BaseModel]] = {
    "node-runtime.json": NodeRuntimeManifest,
    "models.json": ModelCatalog,
    "mcp-servers.json": McpServerCatalog,
    "egress.json": EgressPolicy,
    "observability.json": ObservabilityConfig,
    "hooks.json": HookBindings,
    "error-catalog.json": ErrorCatalog,
    "templates.json": TemplateNamespaces,
    "run-states.json": RunStateMachine,
    "dialects.json": DialectRules,
    "builder-registry.json": BuilderRegistry,
}

__all__ = [
    "RESOURCE_FILES",
    "BuilderRegistry",
    "DialectRules",
    "EgressPolicy",
    "ErrorCatalog",
    "HookBindings",
    "McpServerCatalog",
    "ModelCatalog",
    "NodeRuntimeManifest",
    "ObservabilityConfig",
    "RunStateMachine",
    "RuntimeSettings",
    "TemplateNamespaces",
]
