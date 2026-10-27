"""Runtime resources: settings with their environment overlay, and every other resource file."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel

from zonic_runtime.models.hooks.binding import HookBindings
from zonic_runtime.models.resources import (
    BuilderRegistry,
    DialectRules,
    EgressPolicy,
    ErrorCatalog,
    McpServerCatalog,
    ModelCatalog,
    NodeRuntimeManifest,
    ObservabilityConfig,
    RunStateMachine,
    RuntimeSettings,
    TemplateNamespaces,
)

from .env import EnvironmentModel
from .resources import deep_merge, read_json, validate_resource


def load_with_overlay[M: BaseModel](resources_dir: Path, file_name: str, env_name: str, model: type[M]) -> M:
    """``<name>.json`` deep-merged with ``<name>.<env>.json`` when it exists, then validated."""
    base_path = resources_dir / file_name
    data = read_json(base_path)
    stem, _, suffix = file_name.rpartition(".")
    overlay_path = resources_dir / f"{stem}.{env_name}.{suffix}"
    source = str(base_path)
    if overlay_path.exists():
        data = deep_merge(data, read_json(overlay_path))
        source = f"{base_path} + {overlay_path.name}"
    return validate_resource(data, model, source=source)


def load_settings(resources_dir: Path, env_name: str) -> RuntimeSettings:
    return load_with_overlay(resources_dir, "settings.json", env_name, RuntimeSettings)


@dataclass(frozen=True, slots=True)
class RuntimeResources:
    """Everything the runtime reads from ``resources/``, immutable after start-up."""

    settings: RuntimeSettings
    manifest: NodeRuntimeManifest
    models: ModelCatalog
    mcp_servers: McpServerCatalog
    egress: EgressPolicy
    observability: ObservabilityConfig
    hooks: HookBindings
    errors: ErrorCatalog
    templates: TemplateNamespaces
    run_states: RunStateMachine
    dialects: DialectRules
    builder: BuilderRegistry


def load_resources(env: EnvironmentModel) -> RuntimeResources:
    d, e = env.resources_dir, env.env
    return RuntimeResources(
        settings=load_settings(d, e),
        manifest=load_with_overlay(d, "node-runtime.json", e, NodeRuntimeManifest),
        models=load_with_overlay(d, "models.json", e, ModelCatalog),
        mcp_servers=load_with_overlay(d, "mcp-servers.json", e, McpServerCatalog),
        egress=load_with_overlay(d, "egress.json", e, EgressPolicy),
        observability=load_with_overlay(d, "observability.json", e, ObservabilityConfig),
        hooks=load_with_overlay(d, "hooks.json", e, HookBindings),
        errors=load_with_overlay(d, "error-catalog.json", e, ErrorCatalog),
        templates=load_with_overlay(d, "templates.json", e, TemplateNamespaces),
        run_states=load_with_overlay(d, "run-states.json", e, RunStateMachine),
        dialects=load_with_overlay(d, "dialects.json", e, DialectRules),
        builder=load_with_overlay(d, "builder-registry.json", e, BuilderRegistry),
    )
