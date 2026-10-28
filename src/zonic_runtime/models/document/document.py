"""The builder's workflow document, as stored (design Section 5, plan P1-01).

The models accept every document the builders write: Strands builder 2.0, the
FastAPI and Django builders' 1.0 (no ``schemaVersion``), and the InSite platform
imports. Unknown keys are kept, because the builder's round trip must preserve
them, and are listed by ``WorkflowDocument.unknown_key_report`` for the loader to
report. Node configuration stays a plain object here; P1-02 types it per kind.

Node types are not enumerated in code (design rule 1): ``WorkflowNode.type`` is a
string the loader checks against ``node-runtime.json``, whose node types a contract
test keeps equal to the builder schema's.
"""

from __future__ import annotations

from typing import Any, ClassVar

from pydantic import Field

from zonic_runtime.models.base import ZonicDocumentModel

#: A document without ``schemaVersion`` was written by the 1.0 builders (design 5.1).
DEFAULT_DOCUMENT_VERSION = "1.0"

#: The node configuration key naming the model (agent and LLM nodes, swarm members).
MODEL_KEY = "model"

#: Canvas-only keys the builders write on nodes and edges. They are layout, not unknown
#: content, so they are not reported; the loader strips them (design 5.1, step 5).
CANVAS_KEYS = frozenset({"selected", "dragging", "deletable", "focusable", "measured", "resizing"})


class Point(ZonicDocumentModel):
    x: float | None = None
    y: float | None = None


class VariableDefinition(ZonicDocumentModel):
    """One typed flow variable. The builder keys it by name; the platform lists it with ``name``."""

    name: str | None = None
    type: str | None = None
    default: Any = None
    description: str | None = None
    required: bool | None = None
    ref_id: str | None = None
    id: str | None = None


class SystemParam(ZonicDocumentModel):
    name: str
    description: str | None = None
    default_value: Any = None


class WorkflowNode(ZonicDocumentModel):
    known_extra_keys: ClassVar[frozenset[str]] = CANVAS_KEYS

    id: str = Field(min_length=1)
    type: str = Field(min_length=1)
    name: str | None = None
    description: str | None = None
    config: dict[str, Any] = Field(default_factory=dict)
    properties: dict[str, Any] | None = None
    variable_updates: list[Any] | None = None
    position: Point | None = None
    position_absolute: Point | None = None
    width: float | None = None
    height: float | None = None
    parent_id: str | None = None
    data: dict[str, Any] | None = None
    style: dict[str, Any] | None = None

    @property
    def label(self) -> str:
        """The name the builder shows, else the ID."""
        return self.name or self.id


class WorkflowEdge(ZonicDocumentModel):
    known_extra_keys: ClassVar[frozenset[str]] = CANVAS_KEYS

    id: str | None = None
    source: str = Field(min_length=1)
    target: str = Field(min_length=1)
    source_handle: str | None = None
    target_handle: str | None = None
    type: str | None = None
    data: dict[str, Any] | None = None
    style: dict[str, Any] | None = None


class DocumentLimits(ZonicDocumentModel):
    """A document's own loop limits, capped by ``settings.engine.maxLimits`` (design 6.4).

    Runtime-only for now: the builder does not write it (the Node.js runtime read it too).
    """

    max_visits_per_node: int | None = Field(default=None, gt=0)
    max_steps: int | None = Field(default=None, gt=0)


class WorkflowDocument(ZonicDocumentModel):
    schema_version: str | None = Field(default=None, pattern=r"^[0-9]+\.[0-9]+$")
    agentic_workflow_id: str | None = Field(default=None, min_length=1)
    name: str | None = None
    display_name: str | None = None
    description: str | None = None
    bpc: str | None = None
    edit_as: str | None = None
    invocation_type: str | None = None
    app_id: str | None = None
    module_id: str | None = None
    environment: str | None = None
    version: str | None = None
    is_disabled: bool | None = None
    is_available_for_migration: bool | None = None
    enable_for_auto_invoke: bool | None = None
    include_thoughts: bool | None = None
    #: Builder: ``{name: VariableDefinition}``. Platform: ``{"flow": [VariableDefinition, ...]}``.
    variables: dict[str, VariableDefinition | list[VariableDefinition]] | None = None
    interface: dict[str, Any] | None = None
    security: dict[str, Any] | None = None
    skills: list[Any] | None = None
    publish_channels: list[Any] | None = None
    semantic_search_enabled: bool | None = None
    semantic_domain: str | None = None
    semantic_keywords: str | None = None
    semantic_use_cases: str | None = None
    system_params: list[SystemParam] | None = None
    limits: DocumentLimits | None = None
    nodes: list[WorkflowNode]
    edges: list[WorkflowEdge]

    @property
    def document_version(self) -> str:
        return self.schema_version or DEFAULT_DOCUMENT_VERSION

    def variable_definitions(self) -> dict[str, VariableDefinition]:
        """Flow variables by name, from either shape of ``variables``."""
        found: dict[str, VariableDefinition] = {}
        for key, value in (self.variables or {}).items():
            if isinstance(value, VariableDefinition):
                found[value.name or key] = value
                continue
            for item in value:
                if item.name:
                    found[item.name] = item
        return found

    def node(self, node_id: str) -> WorkflowNode | None:
        return next((n for n in self.nodes if n.id == node_id), None)

    def empty_model_nodes(self) -> list[str]:
        """IDs of nodes whose ``model`` is present but empty, as the builder's templates write it.

        The normalizer (P1-03) resolves them to the catalog's ``defaultModel`` with a warning.
        """
        return [n.id for n in self.nodes if MODEL_KEY in n.config and not n.config[MODEL_KEY]]

    def unknown_key_report(self) -> list[str]:
        """Every unknown key as a path (``nodes[3].foo``), for the loader to report."""
        paths = list(self.unknown_keys())
        for i, node in enumerate(self.nodes):
            paths += [f"nodes[{i}].{k}" for k in node.unknown_keys()]
        for i, edge in enumerate(self.edges):
            paths += [f"edges[{i}].{k}" for k in edge.unknown_keys()]
        return paths


class WorkflowTemplate(ZonicDocumentModel):
    """A builder template file: ``{name, title, description, document}``."""

    name: str
    title: str | None = None
    description: str | None = None
    document: WorkflowDocument
