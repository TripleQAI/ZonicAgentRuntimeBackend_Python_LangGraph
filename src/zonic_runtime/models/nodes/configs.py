"""One configuration model per node kind (design 8.4, plan P1-02): 17 kinds.

Fields are what the builders write, in both dialects (the builder's and the InSite
platform's); the normalizer (P1-03) maps the platform's spellings onto the builder's.
Model references stay as written (a catalog code string or the platform's model
object); P1-03 turns them into ``ModelRef``.
"""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import Field

from zonic_runtime.models.markers import SecretRef, TemplateField
from zonic_runtime.models.nodes.base import NodeConfigBase, RuleBranch

#: A model as written: a catalog code or alias, or the platform's model object.
ModelValue = str | dict[str, Any] | None


class EntryNodeConfig(NodeConfigBase):
    interface_inputs: list[Any] | None = None
    label: str | None = None
    node_type: str | None = None


class LlmNodeConfig(NodeConfigBase):
    model: ModelValue = None
    messages: Annotated[list[Any] | None, TemplateField()] = None
    tools: list[Any] | None = None
    response_format: str | None = None
    temperature: float | None = None
    max_tokens: int | None = None
    error_handling: bool | dict[str, Any] | None = None  # canonical: {policy}
    prompt_template: Annotated[list[Any] | None, TemplateField()] = None
    stream_response: bool | None = None
    structured_output: dict[str, Any] | None = None
    user_query_mapping: Annotated[str | None, TemplateField()] = None
    user_query_vars: list[Any] | None = None


class AgentNodeConfig(NodeConfigBase):
    model: ModelValue = None
    strategy: str | None = None
    agent_strategy: str | None = None
    system_prompt: Annotated[str | None, TemplateField()] = None
    messages: Annotated[list[Any] | None, TemplateField()] = None
    tools: list[Any] | None = None
    routing_tools: list[Any] | None = None
    handoff_target: list[str] | None = None
    max_iterations: int | None = None
    include_thoughts: bool | None = None
    prompt_template: Annotated[list[Any] | None, TemplateField()] = None
    structured_output: dict[str, Any] | None = None
    user_query_mapping: Annotated[str | None, TemplateField()] = None
    stream_response: bool | None = None
    connectors: list[Any] | None = None
    libraries: list[Any] | None = None
    resources: list[Any] | None = None
    skills: list[Any] | None = None


class MultiAgentNodeConfig(NodeConfigBase):
    members: list[str] = Field(default_factory=list)
    entry_point: str | None = None
    task_mapping: Annotated[str | None, TemplateField()] = None
    max_handoffs: int | None = None
    max_iterations: int | None = None
    execution_timeout_seconds: float | None = None
    node_timeout_seconds: float | None = None
    repetitive_handoff_detection_window: int | None = None
    repetitive_handoff_min_unique_agents: int | None = None


class RouterNodeConfig(NodeConfigBase):
    rules: list[RuleBranch] = Field(default_factory=list)


class GuardrailNodeConfig(NodeConfigBase):
    input_mapping: Annotated[list[Any] | None, TemplateField()] = None
    policy: str | None = None
    action: str | None = None


class InterruptNodeConfig(NodeConfigBase):
    """Approval and human input."""

    message: Annotated[str | None, TemplateField()] = None
    prompt: Annotated[str | None, TemplateField()] = None
    approvers: list[Any] | None = None
    timeout_hours: float | None = None
    response_variable: str | None = None
    input_type: str | None = None
    required: bool | None = None


class SubflowNodeConfig(NodeConfigBase):
    agentic_workflow_id: str | None = Field(default=None, min_length=1)
    agentic_workflow_id_mode: str | None = None
    inputs: Annotated[list[Any] | dict[str, Any] | None, TemplateField()] = None
    agent_info: dict[str, Any] | None = None
    app_info: dict[str, Any] | None = None
    module_info: dict[str, Any] | None = None


class VariableNodeConfig(NodeConfigBase):
    updates: Annotated[list[Any] | None, TemplateField()] = None


class McpExecuteNodeConfig(NodeConfigBase):
    """Database query through the MCP ``execute`` tool."""

    connection_id: str | None = None
    database_type: str | None = None
    query: Annotated[str | None, TemplateField()] = None
    parameters: Annotated[list[Any] | None, TemplateField()] = None
    max_rows: int | None = None
    timeout_seconds: float | None = None
    output_key: str | None = None
    write_to: str | None = None
    row_schema: Any = None


class HttpNodeConfig(NodeConfigBase):
    url: Annotated[str | None, TemplateField()] = None
    method: str | None = None
    headers: Annotated[list[Any] | None, TemplateField()] = None
    body: Annotated[str | None, TemplateField()] = None
    timeout_seconds: float | None = None


class ToolNodeConfig(NodeConfigBase):
    tool_id: str | None = None
    tool_name: str | None = None
    tool_type: str | None = None
    server_id: str | None = None
    type: str | None = None
    title: str | None = None
    schema_: dict[str, Any] | None = Field(default=None, alias="schema")
    parameters: Annotated[dict[str, Any] | None, TemplateField()] = None
    inputs: Annotated[Any, TemplateField()] = None
    settings: dict[str, Any] | None = None
    enabled: bool | None = None
    return_direct: bool | None = None
    handoff_target: list[str] | None = None
    meta: dict[str, Any] | None = Field(default=None, alias="_meta")


class ExternalAgentNodeConfig(NodeConfigBase):
    endpoint: Annotated[str | None, TemplateField()] = None
    auth_type: str | None = None
    api_key: Annotated[str | None, SecretRef()] = None
    input_mapping: Annotated[list[Any] | None, TemplateField()] = None
    timeout_seconds: float | None = None
    retries: int | None = None


class SandboxNodeConfig(NodeConfigBase):
    """Script node; runs only when ``features.scriptNode`` is on."""

    code: str | None = None
    language: str | None = None
    inputs: Annotated[list[Any] | dict[str, Any] | None, TemplateField()] = None
    outputs: dict[str, Any] | None = None
    input_schema: dict[str, Any] | None = None


class ViewNodeConfig(NodeConfigBase):
    view_mode: str | None = None
    html_template: Annotated[str | None, TemplateField()] = None
    template_config: Annotated[dict[str, Any] | str | None, TemplateField()] = None
    output_variable: str | None = None
    write_to: str | None = None


class OutputNodeConfig(NodeConfigBase):
    output_mapping: Annotated[list[Any] | dict[str, Any] | None, TemplateField()] = None
    output_key: str | None = None
    write_to: str | None = None
    format: str | None = None


class IgnoreNodeConfig(NodeConfigBase):
    """Note: never compiled."""

    text: str | None = None
    color: str | None = None
