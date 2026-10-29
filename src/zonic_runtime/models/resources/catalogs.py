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


#: Langfuse's usage and cost keys: prompt tokens, completion tokens, prompt tokens from the cache.
USAGE_INPUT, USAGE_OUTPUT = "input", "output"  # principle: allow rule-1-node-type langfuse-usage-keys
USAGE_CACHE_READ = "input_cache_read"


class ModelPrice(ZonicResourceModel):
    """The provider's list price per million tokens; traces and run usage are costed with it."""

    currency: Literal["USD"] = "USD"
    input_per_million: float = Field(ge=0)
    output_per_million: float = Field(ge=0)
    #: Prompt tokens served from the provider's cache; the input price when not given.
    cached_input_per_million: float | None = Field(default=None, ge=0)

    def cost(self, input_tokens: int, output_tokens: int, cached_input_tokens: int = 0) -> dict[str, float]:
        """``{input, output, total}`` (and ``input_cache_read`` when cached tokens are given), in the
        currency, rounded to 8 decimals. ``input_tokens`` excludes cached tokens."""
        cached_price = (
            self.cached_input_per_million
            if self.cached_input_per_million is not None
            else self.input_per_million
        )
        parts = {
            USAGE_INPUT: input_tokens * self.input_per_million / 1_000_000,
            USAGE_OUTPUT: output_tokens * self.output_per_million / 1_000_000,
        }
        if cached_input_tokens:
            parts[USAGE_CACHE_READ] = cached_input_tokens * cached_price / 1_000_000
        cost = {k: round(v, 8) for k, v in parts.items()}
        cost["total"] = round(sum(parts.values()), 8)
        return cost


class ModelEntry(ZonicResourceModel):
    provider: ModelProvider
    model_id: str
    enabled: bool
    region: str | None = None  # bedrock
    deployment: str | None = None  # azure deployment name
    api_version: str | None = None  # azure
    endpoint_env: str | None = None  # environment field holding the endpoint (azure, openai-compatible)
    #: Environment field holding the API key, when not the provider's own (an OpenAI-compatible
    #: service such as Nebius Token Factory has its own key).
    api_key_env: str | None = None
    supports_native_structured_output: bool
    parameters: ModelParameterBounds
    quota: ModelQuota
    pricing: ModelPrice | None = None


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

    def price_for(self, model: str | None) -> ModelPrice | None:
        """The price of a model as a node or a provider's response names it: a catalog code, an
        alias, or a provider model ID, case-insensitive. A response may name a dated snapshot of
        the model (``gpt-4.1-2025-04-14``): the longest model ID it starts with wins. ``None`` when
        the model has no price."""
        if not model:
            return None
        entry = self.models.get(self.aliases.get(model, model))
        if entry is not None and entry.pricing is not None:
            return entry.pricing
        wanted = model.lower()
        priced = [e for e in self.models.values() if e.pricing is not None]
        matches = [
            e for e in priced if wanted == e.model_id.lower() or wanted.startswith(e.model_id.lower() + "-")
        ]
        best = max(matches, key=lambda e: len(e.model_id), default=None)
        return best.pricing if best is not None else None


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
