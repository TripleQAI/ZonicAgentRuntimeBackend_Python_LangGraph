"""``resources/settings*.json`` (design lever L10).

Every limit, timeout, size and switch of the runtime lives here, per environment.
Fields have no defaults on purpose: a value missing from the settings file is a
start-up error, so no tunable hides in code (principle test, P0-06).
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from zonic_runtime.models.base import ZonicResourceModel
from zonic_runtime.models.registry import exported


class EngineLimitCaps(ZonicResourceModel):
    """The most a document may raise its own limits to."""

    max_visits_per_node: int = Field(gt=0)
    max_steps: int = Field(gt=0)


class EngineSettings(ZonicResourceModel):
    max_visits_per_node: int = Field(gt=0)
    max_steps: int = Field(gt=0)
    supersteps: int = Field(gt=0)  # LangGraph recursion_limit backstop
    child_supersteps: int = Field(gt=0)
    max_limits: EngineLimitCaps
    max_concurrency_per_graph: int = Field(gt=0)
    max_concurrency_per_run: int = Field(gt=0)
    node_timeout_seconds: float = Field(gt=0)
    run_timeout_seconds: float = Field(gt=0)


class SubflowSettings(ZonicResourceModel):
    max_depth: int = Field(gt=0)
    inherit_flow: Literal["none", "declared", "all"]


class InstanceLossPolicy(ZonicResourceModel):
    stream: Literal["fail", "restart_turn"]
    sync: Literal["fail", "restart_turn"]
    async_: Literal["fail", "restart_turn"] = Field(alias="async")


class RunsSettings(ZonicResourceModel):
    max_concurrent_runs: int = Field(gt=0)
    max_active_per_session: int = Field(gt=0)
    max_active_per_user: int = Field(gt=0)
    max_active_per_tenant: int = Field(gt=0)
    sync_wait_seconds: float = Field(gt=0)
    cancel_on_disconnect: bool
    idempotency_ttl_seconds: int = Field(gt=0)
    on_instance_loss: InstanceLossPolicy


class EncryptionSettings(ZonicResourceModel):
    enabled: bool


class StateSettings(ZonicResourceModel):
    save_deadline_milliseconds: int = Field(gt=0)
    max_bytes_per_call: int = Field(gt=0)
    async_checkpoints: bool
    checkpoint_interval_seconds: float = Field(gt=0)
    checkpoints_kept_per_namespace: int = Field(ge=1)
    encryption: EncryptionSettings


class HistorySettings(ZonicResourceModel):
    max_messages: int = Field(gt=0)
    max_tokens: int = Field(gt=0)
    max_inline_tool_result_bytes: int = Field(gt=0)


class PersistenceSettings(ZonicResourceModel):
    flow: list[str]  # undeclared flow keys that still persist between turns


class DatasetSettings(ZonicResourceModel):
    store: Literal["cache", "database"]
    cache_ttl_seconds: int = Field(gt=0)
    chunk_bytes: int = Field(gt=0)


class IoSettings(ZonicResourceModel):
    inline_rows: int = Field(ge=0)
    inline_bytes: int = Field(gt=0)
    max_resolved_bytes: int = Field(gt=0)
    output_validation: Literal["strict", "warn"]
    template_path_unknown: Literal["warning", "error"]


class TemplateSettings(ZonicResourceModel):
    missing: Literal["warn", "error"]
    writable_system_keys: list[str]


class CompatSettings(ZonicResourceModel):
    rule_expression: Literal["unsupported", "parse_simple"]


class InterruptSettings(ZonicResourceModel):
    on_expiry: Literal["cancel", "reject"]
    default_expiry_hours: float = Field(gt=0)


class SwarmSettings(ZonicResourceModel):
    context_mode: Literal["shared", "handoff"]


class ToolSettings(ZonicResourceModel):
    max_concurrent_per_turn: int = Field(gt=0)
    require_approval: list[str]
    builtin_allow_list: list[str]


class HookSettings(ZonicResourceModel):
    document_allowed_types: list[str]
    max_retries_per_node: int = Field(ge=0)
    async_queue_size: int = Field(gt=0)


class SseSettings(ZonicResourceModel):
    heartbeat_seconds: float = Field(gt=0)
    coalesce_milliseconds: int = Field(ge=0)
    coalesce_bytes: int = Field(gt=0)
    max_event_bytes: int = Field(gt=0)


class StreamSettings(ZonicResourceModel):
    replay_buffer_events: int = Field(gt=0)
    replay_retention_seconds: float = Field(gt=0)
    subscriber_queue_events: int = Field(gt=0)


class ShutdownSettings(ZonicResourceModel):
    drain_seconds: float = Field(ge=0)


class AsyncRunSettings(ZonicResourceModel):
    claim_interval_milliseconds: int = Field(gt=0)
    idle_claim_interval_milliseconds: int = Field(gt=0)
    lease_seconds: int = Field(gt=0)
    claim_batch: int = Field(gt=0)
    callback_retry_attempts: int = Field(ge=0)


class PressureSettings(ZonicResourceModel):
    max_event_loop_lag_milliseconds: float = Field(gt=0)
    max_rss_megabytes: float = Field(gt=0)
    sample_interval_milliseconds: int = Field(gt=0)


class GatewaySchemas(ZonicResourceModel):
    runtime: str
    builder: str
    observability: str


class GatewayDatasources(ZonicResourceModel):
    runtime: str
    observability: str


class GatewaySettings(ZonicResourceModel):
    datasources: GatewayDatasources
    schemas: GatewaySchemas
    max_in_flight: int = Field(gt=0)
    timeout_milliseconds: int = Field(gt=0)
    connect_timeout_milliseconds: int = Field(gt=0)
    read_retries: int = Field(ge=0)
    retry_backoff_milliseconds: int = Field(gt=0)
    max_view_rows: int = Field(gt=0)


class CpuSettings(ZonicResourceModel):
    offload_bytes: int = Field(gt=0)
    workers: int = Field(gt=0)


class ModelRuntimeSettings(ZonicResourceModel):
    executor_threads: int = Field(gt=0)
    max_retries: int = Field(ge=0)
    request_timeout_seconds: float = Field(gt=0)


class ResultSettings(ZonicResourceModel):
    fallback: list[Literal["last_agent_text", "last_llm_text"]]


class ExportSettings(ZonicResourceModel):
    max_queue_size: int = Field(gt=0)
    interval_milliseconds: int = Field(gt=0)
    max_observations_per_call: int = Field(gt=0)
    max_bytes_per_call: int = Field(gt=0)
    shutdown_flush_timeout_milliseconds: int = Field(gt=0)
    retry_attempts: int = Field(ge=0)
    retry_backoff_milliseconds: int = Field(gt=0)


class ObservabilitySettings(ZonicResourceModel):
    enabled: bool
    environment: str
    sample_rate: float = Field(ge=0, le=1)
    always_trace_errors: bool
    exporters: list[Literal["gateway", "langfuse"]]
    trace_roles: list[str]
    retention_days: int = Field(gt=0)
    partition_months_ahead: int = Field(ge=0)
    export: ExportSettings


class AuthClaims(ZonicResourceModel):
    subject: str
    tenant: str
    groups: str
    email: str


class AuthSettings(ZonicResourceModel):
    claims: AuthClaims
    jwks_cache_seconds: int = Field(gt=0)
    clock_skew_seconds: int = Field(ge=0)


class RateLimitSettings(ZonicResourceModel):
    per_user_per_minute: int = Field(gt=0)
    per_tenant_per_minute: int = Field(gt=0)


class ApiSettings(ZonicResourceModel):
    docs_enabled: bool
    cors_origins: list[str]
    max_body_bytes: int = Field(gt=0)
    internal_port: int = Field(gt=0, lt=65536)


class CatalogSettings(ZonicResourceModel):
    runtime_channels: list[str]  # builder publishChannels codes that make a workflow runnable (design Q2)
    include_unpublished: bool  # development only: list root workflows without a runtime channel
    excluded_id_prefixes: list[str]  # IDs never listed or run, e.g. builder templates (case F-02)
    cache_seconds: float = Field(ge=0)


class CacheSizes(ZonicResourceModel):
    blueprints: int = Field(gt=0)
    graphs: int = Field(gt=0)
    documents: int = Field(gt=0)
    validators: int = Field(gt=0)


class FeatureFlags(ZonicResourceModel):
    script_node: bool


class InputSettings(ZonicResourceModel):
    max_chars: int = Field(gt=0)


class ContentSettings(ZonicResourceModel):
    inline_row_limit: int = Field(gt=0)


class RetentionSettings(ZonicResourceModel):
    datasets_days: int = Field(gt=0)
    sessions_days: int = Field(gt=0)


@exported("resources")
class RuntimeSettings(ZonicResourceModel):
    document_versions: list[str]  # accepted major versions
    engine: EngineSettings
    subflow: SubflowSettings
    runs: RunsSettings
    state: StateSettings
    history: HistorySettings
    persistence: PersistenceSettings
    datasets: DatasetSettings
    io: IoSettings
    templates: TemplateSettings
    compat: CompatSettings
    interrupts: InterruptSettings
    swarm: SwarmSettings
    tools: ToolSettings
    hooks: HookSettings
    sse: SseSettings
    stream: StreamSettings
    shutdown: ShutdownSettings
    async_runs: AsyncRunSettings
    pressure: PressureSettings
    gateway: GatewaySettings
    cpu: CpuSettings
    models: ModelRuntimeSettings
    result: ResultSettings
    observability: ObservabilitySettings
    auth: AuthSettings
    rate_limit: RateLimitSettings
    api: ApiSettings
    catalog: CatalogSettings
    caches: CacheSizes
    features: FeatureFlags
    input: InputSettings
    content: ContentSettings
    retention: RetentionSettings
