"""``ModelFactory``: a node's ``ModelRef`` to a LangChain chat model (design 8.5; plan P2-02).

The node's model is resolved through ``resources/models.json``: a catalog code, an alias, or a
provider model ID; an empty reference is the catalog default (the normalizer has already warned,
case B-02). A model the catalog does not list, or lists as disabled, is ``MODEL_NOT_ALLOWED``
(case N-08). Parameters come from the node, then the reference, bounded by the entry; retries
and the request timeout come from ``settings.models``.

Nodes whose tokens must not stream (manifest ``streamTokens: false``, or the node's
``streamResponse: false``) get a model tagged ``nostream``. The tag is on the model, not on the
invocation, so the node's tool events still stream (case S-09).

Models are built by a ``ModelBuilder`` per provider; tests pass scripted builders.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from types import MappingProxyType
from typing import Any

from langchain.chat_models import init_chat_model
from langchain_core.language_models import BaseChatModel
from langgraph.constants import TAG_NOSTREAM

from zonic_runtime.engine.agents.governor import ModelGovernor, ModelGovernors
from zonic_runtime.errors import ZonicError
from zonic_runtime.models.dialects.refs import ModelRef
from zonic_runtime.models.resources.catalogs import ModelCatalog, ModelEntry
from zonic_runtime.models.resources.settings import ModelRuntimeSettings

#: ``(entry, keyword arguments) -> model``; the arguments are already bounded.
ModelBuilder = Callable[[ModelEntry, dict[str, Any]], BaseChatModel]
#: Reads an environment field by name (``ModelEntry.endpoint_env``).
EnvironmentReader = Callable[[str], str | None]
#: The shared Bedrock runtime client of a region (``integrations/http/pools.py``).
BedrockClient = Callable[[str | None], Any]
#: The shared ``httpx.AsyncClient`` of an upstream key (``HttpPools.get``), for the OpenAI SDKs.
HttpClient = Callable[[str], Any]
#: Providers whose SDK takes an ``http_async_client``.
_HTTPX_PROVIDERS = frozenset({"openai", "azure_openai"})


#: The environment field holding each key-based provider's API key (``ZONIC_<FIELD>``); Bedrock
#: uses the process's AWS credentials instead.
_API_KEYS = MappingProxyType(
    {"azure_openai": "azure_openai_api_key", "openai": "openai_api_key", "anthropic": "anthropic_api_key"}
)


def _provider_arguments(entry: ModelEntry, environment: EnvironmentReader) -> dict[str, Any]:
    endpoint = environment(entry.endpoint_env) if entry.endpoint_env else None
    key = environment(_API_KEYS[entry.provider]) if entry.provider in _API_KEYS else None
    match entry.provider:
        case "bedrock_converse":
            return {"region_name": entry.region} if entry.region else {}
        case "azure_openai":
            found = {
                "azure_deployment": entry.deployment,
                "api_version": entry.api_version,
                "azure_endpoint": endpoint,
                "api_key": key,
            }
        case "openai":  # endpointEnv: an OpenAI-compatible endpoint
            found = {"base_url": endpoint, "api_key": key}
        case _:
            found = {"api_key": key}
    return {k: v for k, v in found.items() if v is not None}


def provider_builders(
    environment: EnvironmentReader, bedrock: BedrockClient | None = None, http: HttpClient | None = None
) -> Mapping[str, ModelBuilder]:
    """``init_chat_model`` per provider. Bedrock models share one client per region (sized to
    ``models.executorThreads``, SP-5) and take no client timeout or retry count, which botocore's
    configuration sets. OpenAI and Azure OpenAI models share one ``httpx.AsyncClient`` per endpoint
    from the process's pools (design 11.3), not the SDK's process-wide default client, which stays
    bound to the first event loop that used it."""

    def build(entry: ModelEntry, arguments: dict[str, Any]) -> BaseChatModel:
        if entry.provider == "bedrock_converse":
            arguments = {k: v for k, v in arguments.items() if k not in ("timeout", "max_retries")}
            if bedrock is not None:
                arguments["client"] = bedrock(entry.region)
        if entry.provider in _HTTPX_PROVIDERS and http is not None:
            endpoint = environment(entry.endpoint_env) if entry.endpoint_env else None
            client = http(f"model:{entry.provider}:{endpoint or ''}")
            # With its own client the SDK no longer streams usage by default: the trace's usage
            # (and the run's cost) need it.
            arguments = {**arguments, "http_async_client": client, "stream_usage": True}
        model = init_chat_model(
            entry.model_id,
            model_provider=entry.provider,
            **_provider_arguments(entry, environment),
            **arguments,
        )
        if not isinstance(model, BaseChatModel):
            raise ZonicError("MODEL_NOT_ALLOWED", model=entry.model_id, reason="not a chat model")
        return model

    return dict.fromkeys(("bedrock_converse", "azure_openai", "anthropic", "openai"), build)


def _bound(value: float | None, low: float, high: float) -> float | None:
    return None if value is None else min(max(value, low), high)


class ModelFactory:
    def __init__(
        self,
        catalog: ModelCatalog,
        settings: ModelRuntimeSettings,
        builders: Mapping[str, ModelBuilder],
        governors: ModelGovernors | None = None,
    ) -> None:
        self._catalog = catalog
        self._settings = settings
        self._builders = dict(builders)
        #: Process-wide limits per catalog model (design 11.2).
        self.governors = governors or ModelGovernors()

    def governor(self, ref: ModelRef | None) -> ModelGovernor:
        code = self.code(ref)
        return self.governors.get(code, self._catalog.models[code].quota)

    def code(self, ref: ModelRef | None) -> str:
        """The catalog code of a reference; ``MODEL_NOT_ALLOWED`` when there is none."""
        wanted = (ref.model_id.strip() if ref is not None else "") or self._catalog.default_model
        models = self._catalog.models
        code = wanted if wanted in models else self._catalog.aliases.get(wanted)
        if code is None:
            code = next((c for c, e in models.items() if e.model_id == wanted), None)
        if code is None:
            raise ZonicError("MODEL_NOT_ALLOWED", model=wanted, reason="not in models.json")
        code = self._catalog.substitutes.get(code, code)  # a stand-in serves it (models.json)
        if not models[code].enabled:
            raise ZonicError("MODEL_NOT_ALLOWED", model=wanted, reason="disabled")
        return code

    def entry(self, ref: ModelRef | None) -> ModelEntry:
        return self._catalog.models[self.code(ref)]

    def create(
        self,
        ref: ModelRef | None,
        *,
        temperature: float | None = None,
        top_p: float | None = None,
        max_tokens: int | None = None,
        stream: bool = True,
    ) -> BaseChatModel:
        entry = self.entry(ref)
        limits = entry.parameters
        arguments: dict[str, Any] = {
            "temperature": _bound(
                temperature if temperature is not None else ref.temperature if ref else None,
                0.0,
                limits.max_temperature,
            ),
            "top_p": _bound(top_p if top_p is not None else ref.top_p if ref else None, 0.0, 1.0),
            "max_tokens": int(
                _bound(
                    max_tokens or (ref.max_tokens if ref else None) or limits.max_tokens, 1, limits.max_tokens
                )
                or limits.max_tokens
            ),
            "max_retries": self._settings.max_retries,
            "timeout": self._settings.request_timeout_seconds,
        }
        if not stream:
            arguments["tags"] = [TAG_NOSTREAM]
        builder = self._builders.get(entry.provider)
        if builder is None:
            raise ZonicError(
                "MODEL_NOT_ALLOWED", model=entry.model_id, reason=f"no builder for {entry.provider}"
            )
        return builder(entry, {k: v for k, v in arguments.items() if v is not None})
