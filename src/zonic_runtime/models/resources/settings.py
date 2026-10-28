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
    #: How long a cancelled or timed-out run waits for its nodes to stop before it ends anyway.
    cancel_grace_seconds: float = Field(gt=0)
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
    #: Each call to the cache service (``ZONIC_CACHE_API_URL``, P3-05).
    cache_timeout_milliseconds: int = Field(gt=0)


class IoSettings(ZonicResourceModel):
    inline_rows: int = Field(ge=0)
    inline_bytes: int = Field(gt=0)
    max_resolved_bytes: int = Field(gt=0)
    #: Rows read for a dataset's column names (every key of the first rows, in first-seen order).
    column_sample_rows: int = Field(gt=0)
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
    #: How often each instance expires the interrupts past their ``expiresAt`` (P4-02).
    expiry_check_seconds: float = Field(gt=0)


class SwarmSharedContext(ZonicResourceModel):
    """The lines a member gets before its input in context mode ``shared``; ``{task}``,
    ``{history}``, ``{members}`` and ``{message}`` are filled in."""

    task: str
    history: str
    members: str
    message: str


class SwarmSettings(ZonicResourceModel):
    context_mode: Literal["shared", "handoff"]
    #: ``config.maxHandoffs`` and ``config.maxIterations`` when a Swarm node leaves them out.
    max_handoffs: int = Field(gt=0)
    max_iterations: int = Field(gt=0)
    shared_context: SwarmSharedContext
    #: The ``ToolMessage`` that pairs a handoff call in the shared history; ``{member}`` is the target.
    handoff_acknowledgement: str


class ToolSettings(ZonicResourceModel):
    max_concurrent_per_turn: int = Field(gt=0)
    require_approval: list[str]
    builtin_allow_list: list[str]


class HookSettings(ZonicResourceModel):
    document_allowed_types: list[str]
    max_retries_per_node: int = Field(ge=0)
    async_queue_size: int = Field(gt=0)
    default_timeout_milliseconds: int = Field(gt=0)
    #: ``cache`` and ``rateLimit`` hooks without the cache service: entries kept in this process.
    cache_max_entries: int = Field(gt=0)
    rate_limit_max_keys: int = Field(gt=0)
    #: Named patterns of personal data, ``/source/flags`` (``mask``, ``guardrail``, ``webhook``).
    patterns: dict[str, str]
    mask_replacement: str
    webhook_mask_patterns: list[str]
    #: ``webhook`` endpoints by name; each must pass the egress allow list (checked at load).
    endpoints: dict[str, str]
    #: Characters of a masked node's streamed text held back so a match split across deltas is masked.
    stream_mask_holdback_chars: int = Field(ge=0)


class GuardrailSettings(ZonicResourceModel):
    """Guardrail nodes and hooks (P3-06, P3-10): the policy model (empty: the catalog default),
    the policies by name, and the Bedrock Guardrails region (empty: off)."""

    model: str
    policies: dict[str, str]
    bedrock_region: str
    default_version: str
    prompt: str
    default_policy: str


class MaintenanceSettings(ZonicResourceModel):
    """The maintenance schedule (P3-11): partitions ahead, trace and runtime retention, rollups."""

    enabled: bool
    interval_minutes: int = Field(gt=0)


class ExternalAgentSettings(ZonicResourceModel):
    """External agent nodes (P3-06): the wait before retry ``n`` is ``n`` times this."""

    retry_backoff_milliseconds: int = Field(ge=0)


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
    #: The header of a completion callback's signature: ``sha256=<hex HMAC-SHA256 of the body>``.
    callback_signature_header: str = Field(min_length=1)
    callback_timeout_milliseconds: int = Field(gt=0)
    callback_backoff_milliseconds: int = Field(ge=0)


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
    #: Model calls of an LLM node that has tools (its short agent loop).
    llm_max_turns: int = Field(gt=0)


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
    #: The score a message's feedback becomes on its run's trace (P3-11).
    feedback_score_name: str = Field(min_length=1)
    #: ``GET /v1/observability/usage`` without ``from``: this many days back.
    usage_default_days: int = Field(gt=0)
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
    #: Groups (the ``groups`` claim) that may read agents' internal messages.
    internal_message_roles: list[str]


class RateLimitSettings(ZonicResourceModel):
    per_user_per_minute: int = Field(gt=0)
    per_tenant_per_minute: int = Field(gt=0)


class ApiSettings(ZonicResourceModel):
    docs_enabled: bool
    cors_origins: list[str]
    max_body_bytes: int = Field(gt=0)
    internal_port: int = Field(gt=0, lt=65536)
    #: How other instances reach this one's internal port (its run events, P4-05); empty is
    #: ``http://<host name>:<internalPort>``.
    internal_address: str
    default_page_size: int = Field(gt=0)
    max_page_size: int = Field(gt=0)
    #: A new session's title: the start of its first message.
    session_title_chars: int = Field(gt=0)


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


class SandboxSettings(ZonicResourceModel):
    """The Script node's sandbox service (P3-07): a Piston API at ``ZONIC_SANDBOX_URL``. Without
    one, Script nodes fail with ``FEATURE_DISABLED``. ``languages`` maps a node's language to the
    sandbox's runtime name; any other language is ``SANDBOX_ERROR``."""

    run_timeout_milliseconds: int = Field(gt=0)
    max_output_bytes: int = Field(gt=0)
    languages: dict[str, str]


class InputSettings(ZonicResourceModel):
    max_chars: int = Field(gt=0)


class ContentSettings(ZonicResourceModel):
    """Content parts (P3-08): inline table rows, the ECharts option keys a chart may use, and the
    tags and attributes a sanitized ``html`` part keeps (``*`` for every tag)."""

    inline_row_limit: int = Field(gt=0)
    chart_option_keys: list[str]
    html_allowed_tags: list[str]
    html_allowed_attributes: dict[str, list[str]]


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
    guardrails: GuardrailSettings
    external_agents: ExternalAgentSettings
    maintenance: MaintenanceSettings
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
    sandbox: SandboxSettings
    input: InputSettings
    content: ContentSettings
    retention: RetentionSettings
