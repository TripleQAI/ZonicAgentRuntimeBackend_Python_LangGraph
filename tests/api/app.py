"""The app with fakes (plan P2-03): the in-memory gateway with the runtime schema, a test issuer
for Cognito-shaped tokens, and scripted models. Every API test drives it through HTTP only.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from typing import Any

import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

from tests.fakes.clock import FakeClock
from tests.fakes.in_memory_gateway import InMemoryGateway
from tests.fakes.runtime_schema import RuntimeSchema
from tests.fakes.scripted_chat_model import ScriptedChatModel, Turn
from tests.kinds.agents import ScriptedModels
from zonic_runtime.api.auth import StaticKeys, TokenVerifier
from zonic_runtime.api.deps import AppDeps
from zonic_runtime.config.env import EnvironmentModel
from zonic_runtime.config.settings import RuntimeResources, load_resources
from zonic_runtime.engine.agents.agent_factory import AgentServices
from zonic_runtime.engine.node_template import ServicePorts
from zonic_runtime.errors import ZonicError
from zonic_runtime.integrations.egress.guard import EgressGuard
from zonic_runtime.integrations.langfuse.tracing import GatewayTracing
from zonic_runtime.main import create_app
from zonic_runtime.run.state.dataset_store import DatasetStore

DEV = load_resources(EnvironmentModel(env="dev"))
ISSUER, AUDIENCE, KID = "https://cognito-idp.test/pool", "client-1", "test-key"
_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
T0 = "2026-10-25T04:49:28Z"

#: Start -> LLM -> Output: the answer is the LLM's text.
CHAT: dict[str, Any] = {
    "schemaVersion": "2.0",
    "name": "chat",
    "nodes": [
        {"id": "start", "type": "start", "config": {}},
        {
            "id": "llm",
            "type": "llm",
            "name": "llm",
            "config": {"messages": [{"role": "user", "content": "{{system.userQuery}}"}]},
        },
        {
            "id": "out",
            "type": "output",
            "config": {"outputMapping": [{"target": "answer", "value": "{{nodes.llm.output.text}}"}]},
        },
    ],
    "edges": [{"source": "start", "target": "llm"}, {"source": "llm", "target": "out"}],
}


def token(
    subject: str = "alice",
    *,
    groups: Sequence[str] = (),
    tenant: str | None = "t1",
    audience: str = AUDIENCE,
    issuer: str = ISSUER,
    expires_in: float = 3600,
    key: Any = None,
) -> str:
    claims: dict[str, Any] = {
        "sub": subject,
        "iss": issuer,
        "client_id": audience,
        "token_use": "access",
        "exp": int(time.time() + expires_in),
        "cognito:groups": list(groups),
        "email": f"{subject}@example.test",
    }
    if tenant is not None:
        claims["custom:tenant"] = tenant
    return jwt.encode(claims, key or _KEY, algorithm="RS256", headers={"kid": KID})


def auth(subject: str = "alice", **kwargs: Any) -> dict[str, str]:
    return {"Authorization": f"Bearer {token(subject, **kwargs)}"}


def _snake(record: dict[str, Any]) -> dict[str, Any]:
    return {re.sub(r"(?<!^)(?=[A-Z])", "_", k).lower(): v for k, v in record.items()}


def _day(value: Any) -> str:
    return str(value or "")[:10]


def _tokens(found: list[dict[str, Any]], key: str) -> int:
    return sum(int((o.get("usageDetails") or {}).get(key) or 0) for o in found)


def _cost(found: list[dict[str, Any]]) -> float:
    return sum(float(o.get("totalCost") or 0) for o in found)


@dataclass
class ObsStore:
    """``agent_observability`` on the fake gateway (P2-08, P3-11): ``sp_obs_ingest`` (traces merged,
    observations inserted once, O-02), ``sp_obs_score_upsert``, the trace, observation and score
    views with the trace's user and tenant, the daily rollups, and the maintenance procedures."""

    traces: dict[str, dict[str, Any]] = field(default_factory=dict)
    observations: dict[str, dict[str, Any]] = field(default_factory=dict)
    scores: dict[str, dict[str, Any]] = field(default_factory=dict)
    maintenance: list[tuple[str, list[Any]]] = field(default_factory=list)

    def install(self, gateway: InMemoryGateway) -> None:
        async def ingest(_: InMemoryGateway, args: list[Any]) -> list[dict[str, Any]]:
            traces, observations = json.loads(args[0]), json.loads(args[1])
            scores = json.loads(args[2]) if len(args) > 2 and args[2] else []
            for trace in traces:
                found = self.traces.setdefault(trace["traceId"], {})
                found.update({k: v for k, v in trace.items() if v is not None})
            for observation in observations:
                self.observations.setdefault(observation["observationId"], observation)
            for score in scores:
                self.scores[score["scoreId"]] = score
            counts = {"o_trace_count": len(traces), "o_observation_count": len(observations)}
            return [{**counts, "o_score_count": len(scores)}]

        async def upsert(_: InMemoryGateway, args: list[Any]) -> list[dict[str, Any]]:
            score = json.loads(args[0]) if isinstance(args[0], str) else dict(args[0])
            if score["traceId"] not in self.traces:
                raise ZonicError("NOT_FOUND", reason="trace")
            self.scores[score["scoreId"]] = score
            return [{"o_score_id": score["scoreId"]}]

        def step(name: str, out: dict[str, Any]) -> Any:
            async def run(_: InMemoryGateway, args: list[Any]) -> list[dict[str, Any]]:
                self.maintenance.append((name, list(args)))
                return [out]

            return run

        on = gateway.on_procedure
        on("observability", "sp_obs_ingest", ingest)
        on("observability", "sp_obs_score_upsert", upsert)
        on("observability", "sp_obs_partition_ensure", step("partitions", {"o_created": 0}))
        purged = {"o_partitions_dropped": 0, "o_traces_deleted": 0}
        on("observability", "sp_obs_retention_purge", step("traceRetention", purged))
        on("observability", "sp_obs_refresh_rollups", step("rollups", {"o_refreshed": 2}))
        views = (
            "v_obs_trace",
            "v_obs_observation",
            "v_obs_score",
            "v_obs_node_stats_daily",
            "v_obs_model_usage_daily",
        )
        for view in views:
            gateway.define_view("observability", view, getattr(self, view))

    def _owner(self, trace_id: str) -> dict[str, Any]:
        trace = self.traces.get(trace_id, {})
        return {"user_id": trace.get("userId"), "tenant_id": trace.get("tenantId")}

    def v_obs_trace(self, _: InMemoryGateway) -> list[dict[str, Any]]:
        rows = []
        for trace in self.traces.values():
            mine = [o for o in self.observations.values() if o["traceId"] == trace["traceId"]]
            rows.append(
                {
                    **_snake(trace),
                    "observation_count": len(mine),
                    "error_count": sum(1 for o in mine if o.get("level") == "ERROR"),
                    "input_tokens": _tokens(mine, "input"),
                    "output_tokens": _tokens(mine, "output"),
                    "total_cost": _cost(mine),
                }
            )
        return rows

    def v_obs_observation(self, _: InMemoryGateway) -> list[dict[str, Any]]:
        return [{**_snake(o), **self._owner(o["traceId"])} for o in self.observations.values()]

    def v_obs_score(self, _: InMemoryGateway) -> list[dict[str, Any]]:
        return [{**_snake(s), **self._owner(s["traceId"])} for s in self.scores.values()]

    def v_obs_node_stats_daily(self, _: InMemoryGateway) -> list[dict[str, Any]]:
        groups: dict[tuple[str, ...], list[dict[str, Any]]] = {}
        for o in self.observations.values():
            if o.get("nodeId") is None or o.get("level") == "DEBUG":
                continue
            workflow = self.traces.get(o["traceId"], {}).get("agenticWorkflowId") or ""
            key = (_day(o.get("startTime")), workflow, o.get("nodeType") or "", o.get("nodeName") or "")
            groups.setdefault(key, []).append(o)
        rows = []
        for (day, workflow, node_type, node_name), found in sorted(groups.items()):
            errors = sum(1 for o in found if o.get("level") == "ERROR")
            rows.append(
                {
                    "day": day,
                    "agentic_workflow_id": workflow,
                    "node_type": node_type,
                    "node_name": node_name,
                    "executions": len(found),
                    "errors": errors,
                    "error_rate": round(errors / len(found), 4),
                    "input_tokens": _tokens(found, "input"),
                    "output_tokens": _tokens(found, "output"),
                    "total_cost": _cost(found),
                }
            )
        return rows

    def v_obs_model_usage_daily(self, _: InMemoryGateway) -> list[dict[str, Any]]:
        groups: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
        for o in self.observations.values():
            if not o.get("model"):
                continue
            tenant = self.traces.get(o["traceId"], {}).get("tenantId") or ""
            groups.setdefault((_day(o.get("startTime")), o["model"], tenant), []).append(o)
        return [
            {
                "day": day,
                "model": model,
                "tenant_id": tenant,
                "calls": len(found),
                "input_tokens": _tokens(found, "input"),
                "output_tokens": _tokens(found, "output"),
                "total_cost": _cost(found),
            }
            for (day, model, tenant), found in sorted(groups.items())
        ]


@dataclass
class FakeApp:
    deps: AppDeps
    gateway: InMemoryGateway
    schema: RuntimeSchema
    models: ScriptedModels
    clock: FakeClock = field(default_factory=FakeClock)
    obs: ObsStore | None = None

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(self.deps)), base_url="http://runtime.test"
        )

    def twin(self, script: Sequence[Turn] = (), *, address: str = "http://b.runtime.test") -> FakeApp:
        """A second instance on the same gateway (another process): its own caches, saver, event
        bus, registry and (with durable runs) its instance at ``address``; nothing shared but the
        database (cases H-04, Q-10 to Q-14)."""
        resources = _addressed(self.deps.resources, address)
        models = ScriptedModels(ScriptedChatModel(script=list(script)))
        agents = AgentServices.from_resources(resources, builders=models.builders())
        ports = replace(self.deps.runtime.services.ports, subflows=None)
        durable = self.deps.runs.durable is not None
        deps = AppDeps.build(
            resources, self.gateway, self.deps.verifier, agents=agents, ports=ports, durable=durable
        )
        return FakeApp(deps=deps, gateway=self.gateway, schema=self.schema, models=models, clock=self.clock)

    def publish(
        self, workflow_id: str, document: dict[str, Any], *, name: str | None = None, channel: str = "api"
    ) -> None:
        self.gateway.put_rows(
            "runtime",
            "v_agent_catalog",
            [
                {
                    "agentic_workflow_id": workflow_id,
                    "name": name or workflow_id,
                    "updated_at": T0,
                    "publish_channels": [{"code": channel}],
                    "node_count": len((document.get("document") or document)["nodes"]),
                    "runtime_channel": channel,
                }
            ],
        )
        self.gateway.put_rows(
            "builder",
            "v_agent_export",
            [{"agentic_workflow_id": workflow_id, "is_root": True, "export_json": document}],
        )
        self.gateway.put_rows(
            "builder", "v_agent_summary", [{"agentic_workflow_id": workflow_id, "updated_at": T0}]
        )


def _addressed(resources: RuntimeResources, address: str) -> RuntimeResources:
    """The resources of an instance at internal ``address`` (its runs' leases name it)."""
    api = resources.settings.api.model_copy(update={"internal_address": address})
    return replace(resources, settings=resources.settings.model_copy(update={"api": api}))


def fake_verifier(resources: RuntimeResources) -> TokenVerifier:
    """Accepts the tokens ``token()`` signs (the test issuer stands in for Cognito)."""
    return TokenVerifier(
        StaticKeys({KID: _KEY.public_key()}), resources.settings.auth, issuer=ISSUER, audience=AUDIENCE
    )


def make_app(
    script: Sequence[Turn] = (),
    *,
    resources: RuntimeResources = DEV,
    workflows: dict[str, dict[str, Any]] | None = None,
    tracing: bool = False,
    ports: Any = None,
    hook_types: Sequence[Any] = (),
    durable: bool = False,
    **settings: Any,
) -> FakeApp:
    """``tracing``: the production tracer, exporting through the fake gateway's ``sp_obs_ingest``
    (``ObsStore``); the test calls ``deps.tracing.start()`` in its loop and ``aclose()`` at the end."""
    if settings:
        updates = {k: getattr(resources.settings, k).model_copy(update=v) for k, v in settings.items()}
        resources = replace(resources, settings=resources.settings.model_copy(update=updates))
    gateway = InMemoryGateway()
    schema = RuntimeSchema(gateway)
    models = ScriptedModels(ScriptedChatModel(script=list(script)))
    agents = AgentServices.from_resources(resources, builders=models.builders())
    verifier = fake_verifier(resources)
    clock = FakeClock()
    obs = ObsStore() if tracing else None
    if obs is not None:
        obs.install(gateway)
    tracer = GatewayTracing.create(resources, gateway) if tracing else None
    # The dataset store's expiry times come from the fake clock, so a turn's result is reproducible.
    ports = ports or ServicePorts(egress=EgressGuard(resources.egress))
    if ports.datasets is None:
        ports = replace(ports, datasets=DatasetStore(resources.settings, ports.cache, clock=clock))
    if durable:
        resources = _addressed(resources, "http://a.runtime.test")
    deps = AppDeps.build(
        resources,
        gateway,
        verifier,
        agents=agents,
        tracing=tracer,
        ports=ports,
        hook_types=hook_types,
        durable=durable,
    )
    app = FakeApp(deps=deps, gateway=gateway, schema=schema, models=models, clock=clock, obs=obs)
    for workflow_id, document in (workflows if workflows is not None else {"wf-chat": CHAT}).items():
        app.publish(workflow_id, document)
    return app


def sse_events(body: str) -> list[dict[str, Any]]:
    """The ``data`` of each event of an SSE body, in order."""
    return [json.loads(line[len("data: ") :]) for line in body.splitlines() if line.startswith("data: ")]
