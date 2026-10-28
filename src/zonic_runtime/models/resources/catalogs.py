"""``models.json``, ``mcp-servers.json`` and ``egress.json`` (design levers L5 and L11)."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from zonic_runtime.models.base import ZonicResourceModel
from zonic_runtime.models.registry import exported

ModelProvider = Literal["bedrock_converse", "azure_openai", "anthropic", "openai"]


class ModelQuota(ZonicResourceModel):
    max_in_flight: int = Field(gt=0)
    requests_per_minute: int = Field(gt=0)
    tokens_per_minute: int = Field(gt=0)
    max_queue_wait_milliseconds: int = Field(gt=0)


class ModelParameterBounds(ZonicResourceModel):
    max_temperature: float = Field(ge=0)
    max_tokens: int = Field(gt=0)


class ModelEntry(ZonicResourceModel):
    provider: ModelProvider
    model_id: str
    enabled: bool
    region: str | None = None  # bedrock
    deployment: str | None = None  # azure deployment name
    api_version: str | None = None  # azure
    endpoint_env: str | None = None  # environment field holding the endpoint (azure, openai-compatible)
    supports_native_structured_output: bool
    parameters: ModelParameterBounds
    quota: ModelQuota


@exported("resources")
class ModelCatalog(ZonicResourceModel):
    default_model: str
    aliases: dict[str, str] = Field(default_factory=dict)  # document model string -> catalog code
    #: Catalog code -> the code that serves it meanwhile: a stand-in while its provider is not
    #: available (the AWS deployment's OpenAI models while Bedrock waits). Documents keep their model.
    substitutes: dict[str, str] = Field(default_factory=dict)
    models: dict[str, ModelEntry]

    @model_validator(mode="after")
    def _references_exist(self) -> ModelCatalog:
        if self.default_model not in self.models:
            raise ValueError(f"defaultModel {self.default_model!r} is not a catalog entry")
        dangling = sorted(a for a, code in self.aliases.items() if code not in self.models)
        if dangling:
            raise ValueError(f"aliases point at unknown catalog codes: {dangling}")
        unknown = sorted(c for pair in self.substitutes.items() for c in pair if c not in self.models)
        if unknown:
            raise ValueError(f"substitutes name unknown catalog codes: {unknown}")
        return self


class McpServer(ZonicResourceModel):
    url: str  # a literal URL, or 'env:<field>' naming an EnvironmentModel field
    path: str = ""  # appended to the URL, e.g. '/mcp/'
    transport: Literal["streamable_http"]
    enabled: bool
    auth_secret_ref: str | None = None
    max_in_flight: int = Field(gt=0)
    timeout_seconds: float = Field(gt=0)
    execute_tool: str | None = None  # the Zonic MCP server's execute tool name


@exported("resources")
class McpServerCatalog(ZonicResourceModel):
    servers: dict[str, McpServer]
    default_execute_server: str

    @model_validator(mode="after")
    def _default_exists(self) -> McpServerCatalog:
        if self.default_execute_server not in self.servers:
            raise ValueError(f"defaultExecuteServer {self.default_execute_server!r} is not a server")
        return self


@exported("resources")
class EgressPolicy(ZonicResourceModel):
    schemes: list[str]
    hosts: list[str]  # exact hosts, or '*.suffix' wildcards
    ports: list[int]
    block_private_ranges: bool
    follow_redirects: bool
    max_response_bytes: int = Field(gt=0)
    timeout_seconds: float = Field(gt=0)
