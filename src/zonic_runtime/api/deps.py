"""``AppDeps``: the process's composition (design 4.3; plan P2-03).

Built once at start-up from the environment and the resources, or by tests with fakes. Routes
reach it through ``request.app.state.deps``; nothing here belongs to a request. ``start`` (in the
app's lifespan) starts the pressure monitor and installs the bounded blocking-call executor;
``aclose`` drains the runs and closes the clients.
"""

from __future__ import annotations

from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from typing import Any

import httpx
from fastapi import Request
from pydantic import SecretStr

from zonic_runtime.api.auth import JwksKeys, StaticKeys, TokenVerifier, bearer
from zonic_runtime.api.rate_limit import RateLimiter
from zonic_runtime.application.caller import Caller
from zonic_runtime.application.catalog import CatalogService
from zonic_runtime.application.datasets import DatasetService
from zonic_runtime.application.observability import FeedbackService, MaintenanceService, TraceService
from zonic_runtime.application.resume_run import InterruptExpiry, InterruptService
from zonic_runtime.application.runs import DurableRuns, RunService
from zonic_runtime.application.runtime import WorkflowRuntime
from zonic_runtime.application.sessions import SessionService
from zonic_runtime.config.env import EnvironmentModel
from zonic_runtime.config.settings import RuntimeResources, load_resources
from zonic_runtime.engine.agents.agent_factory import AgentServices
from zonic_runtime.engine.node_template import ServicePorts
from zonic_runtime.engine.streaming.source import EventSource
from zonic_runtime.errors import ZonicError
from zonic_runtime.hooks.types import NodeHook
from zonic_runtime.integrations.cache.http_cache_store import HttpCacheStore
from zonic_runtime.integrations.egress.guard import EgressGuard
from zonic_runtime.integrations.gateway.http_gateway import HttpGateway
from zonic_runtime.integrations.gateway.repository import GatewayRepository
from zonic_runtime.integrations.guardrails.bedrock import BedrockGuardrails
from zonic_runtime.integrations.http.pools import BedrockClients, HttpPools, install_blocking_executor
from zonic_runtime.integrations.http.run_events_proxy import RunEventsProxy
from zonic_runtime.integrations.langfuse.tracing import GatewayTracing, langfuse_exporter
from zonic_runtime.integrations.mcp.execute import McpService
from zonic_runtime.integrations.mcp.pool import McpPool
from zonic_runtime.integrations.sandbox.piston import PistonSandbox
from zonic_runtime.integrations.secrets.environment import EnvironmentSecrets
from zonic_runtime.ports.gateway import GatewayPort
from zonic_runtime.ports.tracer import RunTracer
from zonic_runtime.run.callbacks import CompletionCallbacks
from zonic_runtime.run.driver import RunDriver
from zonic_runtime.run.events.bus import Coalescing, RunEventBus
from zonic_runtime.run.executor.claimer import RunClaimer
from zonic_runtime.run.executor.executor import RunExecutor
from zonic_runtime.run.executor.instance import RunInstance
from zonic_runtime.run.executor.leases import RunLeases
from zonic_runtime.run.registry import RunRegistry
from zonic_runtime.run.state.checkpoint_store import CheckpointStore
from zonic_runtime.run.state.dataset_store import DatasetStore
from zonic_runtime.run.state.turn_store import TurnStateStore
from zonic_runtime.runtime.clock import Clock, SystemClock
from zonic_runtime.runtime.metrics import Gauge, RuntimeMetrics
from zonic_runtime.runtime.pressure import PressureMonitor
from zonic_runtime.runtime.process_pool import CpuPool

_JWKS_TIMEOUT_SECONDS = 10.0
#: The pooled client of the run events proxy (P4-05).
_PROXY = "run-events"


@dataclass
class AppDeps:
    resources: RuntimeResources
    gateway: GatewayPort
    verifier: TokenVerifier
    runtime: WorkflowRuntime
    bus: RunEventBus
    registry: RunRegistry
    catalog: CatalogService
    sessions: SessionService
    runs: RunService
    interrupts: InterruptService
    expiry: InterruptExpiry
    datasets: DatasetService
    traces: TraceService
    feedback: FeedbackService
    maintenance: MaintenanceService
    limiter: RateLimiter
    clock: Clock
    executor: RunExecutor
    pressure: PressureMonitor
    metrics: RuntimeMetrics
    cpu: CpuPool
    release: str = "dev"
    pools: HttpPools = field(default_factory=HttpPools)
    #: The production tracer and its gateway exporter (P2-08); ``None`` when tracing is off or a
    #: test passes its own tracer.
    tracing: GatewayTracing | None = None
    #: Whether ``start`` runs the maintenance schedule (the server's deps; never a test app's).
    maintain: bool = False
    #: The durable-run claimer (P4-05): set when runs are durable (the server's deps, opt-in tests).
    claimer: RunClaimer | None = None
    _blocking: ThreadPoolExecutor | None = None

    @classmethod
    def build(
        cls,
        resources: RuntimeResources,
        gateway: GatewayPort,
        verifier: TokenVerifier,
        *,
        agents: AgentServices | None = None,
        source: EventSource | None = None,
        clock: Clock | None = None,
        release: str = "dev",
        tracer: RunTracer | None = None,
        tracing: GatewayTracing | None = None,
        hook_types: Sequence[NodeHook] = (),
        aes_key: bytes | None = None,
        ports: ServicePorts | None = None,
        langfuse_url: str | None = None,
        durable: bool = False,
    ) -> AppDeps:
        """``ports``: the I/O kinds' adapters; by default the egress guard over ``egress.json``."""
        clock = clock or SystemClock()
        settings = resources.settings
        tracer = tracer or (tracing.tracer if tracing is not None else None)
        repository = GatewayRepository(gateway)
        ports = ports or ServicePorts(egress=EgressGuard(resources.egress))
        if ports.datasets is None:
            ports = replace(ports, datasets=DatasetStore(settings, ports.cache, clock=clock))
        runtime = WorkflowRuntime(
            resources, repository, agents=agents, hook_types=hook_types, aes_key=aes_key, ports=ports
        )

        pressure = PressureMonitor(settings.pressure)
        executor = RunExecutor(settings.runs, pressure)
        checkpoints = CheckpointStore(repository)
        instance = RunInstance.of(settings.api) if durable else None
        turns = TurnStateStore(
            repository,
            gateway,
            settings,
            saver=runtime.saver,
            clock=clock,
            checkpoints=checkpoints,
            instance=instance,
        )
        sse = settings.sse
        coalescing = (
            Coalescing(sse.coalesce_milliseconds, sse.coalesce_bytes) if sse.coalesce_milliseconds else None
        )
        bus = RunEventBus(settings.stream, sse.max_event_bytes, clock=clock, coalescing=coalescing)
        registry = RunRegistry()
        driver = RunDriver(resources, source=source, finisher=turns.finish, clock=clock)
        sessions = SessionService(repository, runtime, settings)
        runs = RunService(
            resources,
            runtime,
            driver,
            bus,
            registry,
            turns,
            sessions,
            repository,
            clock=clock,
            tracer=tracer,
            executor=executor,
            checkpoints=checkpoints,
        )
        claimer = None
        if instance is not None:
            leases = RunLeases(repository, instance, settings.async_runs, lambda run_id: runs.lost(run_id))
            callbacks = (
                CompletionCallbacks(ports.egress, ports.secrets, settings.async_runs)
                if ports.egress is not None and ports.secrets is not None
                else None
            )
            runs.durable = DurableRuns(instance=instance, leases=leases, callbacks=callbacks)
            claimer = RunClaimer(
                repository,
                instance,
                settings.async_runs,
                executor,
                runs.run_claimed,
                settings.runs.max_concurrent_runs,
            )
        interrupts = InterruptService(
            resources, runs, checkpoints, repository, runtime.services.schemas, hooks=runtime.hooks
        )
        expiry = InterruptExpiry(resources, runs, checkpoints, repository, interrupts, clock=clock)
        metrics = RuntimeMetrics(_gauges(executor, registry, bus, pressure, agents, tracing))
        deps = cls(
            resources=resources,
            gateway=gateway,
            verifier=verifier,
            runtime=runtime,
            bus=bus,
            registry=registry,
            catalog=CatalogService(runtime),
            sessions=sessions,
            runs=runs,
            interrupts=interrupts,
            expiry=expiry,
            datasets=DatasetService(repository, settings, ports.cache),
            traces=TraceService(repository, gateway, settings, langfuse_url=langfuse_url, clock=clock),
            feedback=FeedbackService(repository, settings),
            maintenance=MaintenanceService(repository, settings, clock),
            limiter=RateLimiter(settings.rate_limit, clock.monotonic),
            clock=clock,
            executor=executor,
            pressure=pressure,
            metrics=metrics,
            cpu=CpuPool(settings.cpu),
            release=release,
            tracing=tracing,
            claimer=claimer,
        )
        if runs.durable is not None:  # another instance's runs: their events through its internal route
            runs.proxy = RunEventsProxy(lambda: deps.pools.get(_PROXY, httpx.AsyncClient))
        return deps

    @classmethod
    def from_environment(cls, env: EnvironmentModel) -> AppDeps:
        resources = load_resources(env)
        settings = resources.settings
        pools = HttpPools()
        keys: Any = (
            JwksKeys(
                env.oidc_jwks_url,
                settings.auth.jwks_cache_seconds,
                client=pools.get("jwks", lambda: httpx.AsyncClient(timeout=_JWKS_TIMEOUT_SECONDS)),
            )
            if env.oidc_jwks_url
            else StaticKeys({})
        )
        verifier = TokenVerifier(keys, settings.auth, issuer=env.oidc_issuer, audience=env.oidc_audience)
        gateway = HttpGateway(settings.gateway, env)
        bedrock = BedrockClients(settings.models)
        agents = AgentServices.from_resources(resources, environment=env, bedrock=bedrock)
        region = settings.guardrails.bedrock_region
        guardrails = BedrockGuardrails(region, bedrock) if region else None
        key = env.checkpoint_aes_key if settings.state.encryption.enabled else None
        if settings.state.encryption.enabled and key is None:
            raise ZonicError(
                "RESOURCE_INVALID", reason="state.encryption is on and CHECKPOINT_AES_KEY is not set"
            )
        aes_key = bytes.fromhex(key.get_secret_value()) if key is not None else None
        langfuse = None
        if "langfuse" in settings.observability.exporters:
            public, secret = env.langfuse_public_key, env.langfuse_secret_key
            if env.langfuse_base_url and public is not None and secret is not None:
                langfuse = langfuse_exporter(
                    env.langfuse_base_url, public.get_secret_value(), secret.get_secret_value()
                )
        tracing = GatewayTracing.create(resources, gateway, langfuse=langfuse)
        sandbox = PistonSandbox(settings.sandbox, env.sandbox_url) if env.sandbox_url else None

        def read(field: str) -> str | None:
            value = getattr(env, field, None)
            if isinstance(value, SecretStr):
                return value.get_secret_value()
            return str(value) if value is not None else None

        mcp = McpService(McpPool(resources.mcp_servers, read))
        cache = (
            HttpCacheStore(env.cache_api_url, settings.datasets, pools.get("cache", httpx.AsyncClient))
            if env.cache_api_url
            else None
        )
        ports = ServicePorts(
            egress=EgressGuard(resources.egress),
            sandbox=sandbox,
            mcp=mcp,
            secrets=EnvironmentSecrets(),
            cache=cache,
            guardrails=guardrails,
        )
        deps = cls.build(
            resources,
            gateway,
            verifier,
            agents=agents,
            aes_key=aes_key,
            tracing=tracing,
            ports=ports,
            langfuse_url=env.langfuse_base_url if langfuse is not None else None,
            durable=True,
        )
        deps.pools = pools
        deps.maintain = True
        return deps

    def start(self) -> None:
        """In the running loop, before the first request."""
        self.pressure.start()
        if self.tracing is not None:
            self.tracing.start()
        if self._blocking is None:
            self._blocking = install_blocking_executor(self.resources.settings.models)
        if self.maintain:
            self.maintenance.start()
            self.expiry.start()
        durable = self.runs.durable
        if durable is not None and self.claimer is not None:
            durable.leases.start()
            self.claimer.start()

    async def aclose(self) -> None:
        """A graceful shutdown: no new claims, this instance's runs drained (P4-05), then the rest."""
        await self.maintenance.aclose()
        await self.expiry.aclose()
        durable = self.runs.durable
        if durable is not None and self.claimer is not None:
            await self.claimer.stop()
            await self.runs.drain(self.resources.settings.shutdown.drain_seconds)
            await durable.leases.aclose()
        await self.registry.drain()
        if self.tracing is not None:  # before the gateway closes: the queued records go out
            await self.tracing.aclose()
        await self.pressure.stop()
        await self.gateway.aclose()
        ports = self.runtime.services.ports
        for adapter in (ports.egress, ports.sandbox, ports.mcp):
            if adapter is not None:
                await adapter.aclose()
        await self.pools.aclose()
        self.cpu.shutdown()
        blocking, self._blocking = self._blocking, None
        if blocking is not None:
            blocking.shutdown(wait=False, cancel_futures=True)

    async def warm(self) -> None:
        """At start-up, after ``start``: list the MCP servers' tools, so the first run does not
        wait and the tool catalog version is known. A server that is down now is retried by the
        first call that needs it."""
        mcp = self.runtime.services.ports.mcp
        if mcp is None:
            return
        try:
            await mcp.tools()
        except ZonicError:
            return

    def ready_checks(self) -> dict[str, str]:
        """Readiness beyond the gateway: an overloaded process (case Q-15), or tracing whose last
        export failed (case L-11), is ``degraded``."""
        checks = {"pressure": "degraded" if self.pressure.overloaded() else "ok"}
        if self.tracing is not None:
            checks["tracing"] = "ok" if self.tracing.healthy else "degraded"
        return checks


def _gauges(
    executor: RunExecutor,
    registry: RunRegistry,
    bus: RunEventBus,
    pressure: PressureMonitor,
    agents: AgentServices | None,
    tracing: GatewayTracing | None,
) -> list[Gauge]:
    """The ``/metrics`` gauges of design 11.2."""
    governors = agents.models.governors if agents is not None else None
    exporter = tracing.exporter if tracing is not None else None

    def permits_in_use() -> float:
        return sum(run.context.permits.in_use for run in registry.runs())

    def by_model(read: str) -> dict[str, float]:
        return {g.model_id: getattr(g, read) for g in governors.all()} if governors is not None else {}

    return [
        Gauge("active_runs", "Runs admitted to this process", lambda: executor.active),
        Gauge("streams_open", "Run streams not yet closed", lambda: bus.active),
        Gauge("node_permits_in_use", "Leaf nodes executing, all runs", permits_in_use),
        Gauge(
            "event_loop_lag_milliseconds", "Last sampled event-loop lag", lambda: pressure.lag_milliseconds
        ),
        Gauge(
            "event_loop_lag_p99_milliseconds",
            "p99 of recent event-loop lag",
            lambda: pressure.percentile(0.99),
        ),
        Gauge("overloaded", "1 while new runs get 503 OVERLOADED", lambda: float(pressure.overloaded())),
        Gauge(
            "model_queue_depth",
            "Model calls waiting for a governor permit",
            lambda: by_model("queued"),
            "model",
        ),
        Gauge("model_in_flight", "Model calls in flight", lambda: by_model("in_flight"), "model"),
        Gauge(
            "trace_records_queued",
            "Trace records waiting for sp_obs_ingest",
            lambda: exporter.queued if exporter else 0,
        ),
        Gauge(
            "trace_records_dropped",
            "Trace records dropped (queue full or refused)",
            lambda: exporter.dropped if exporter else 0,
        ),
    ]


def deps_of(request: Request) -> AppDeps:
    deps = getattr(request.app.state, "deps", None)
    if not isinstance(deps, AppDeps):
        raise RuntimeError("the app was created without AppDeps")
    return deps


async def current_caller(request: Request) -> Caller:
    """Every ``/v1`` route: a verified token, then the caller's rate limits (cases A-02, A-12)."""
    deps = deps_of(request)
    caller = await deps.verifier.verify(bearer(request.headers.get("authorization")))
    deps.limiter.check(caller.subject, caller.tenant)
    return caller
