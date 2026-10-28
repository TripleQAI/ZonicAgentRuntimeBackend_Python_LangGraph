"""Live sanity on the real services (plan P5-03: V-01, V-02, V-04, V-05, F-14; V-03 is in
``test_runtime_live.py``, V-06 runs against the deployed load balancer in ``test_deployed_live.py``).

* Models: OpenAI (``ZONIC_OPENAI_API_KEY``). The documents' Claude models are served by the OpenAI
  stand-ins of ``deploy/aws/resources/models.dev.json``, as on the AWS deployment, until Bedrock is
  available; ``LIVE_BEDROCK=1`` runs Bedrock instead, with the account's credentials (``AWS_PROFILE``).
  Every call is small: one short prompt per model, one run per workflow.
* MCP: the deployed Zonic MCP server (``ZONIC_MCP_BASE_URL``, ``ZONIC_MCP_AUTH_TOKEN``) and the
  open test data behind its ``sample_open_data`` connection.
* Azure OpenAI (Q3) is not configured: its catalog models stay disabled.
"""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from tests.api.app import DEV, auth, make_app, sse_events
from tests.api.test_swarm import SUPPORT, support
from tests.live.conftest import live_environment
from tests.live.test_runtime_live import OWNER, TENANT, open_live
from tests.workflow_fixtures import BUILDER, read_json
from zonic_runtime.config.settings import RuntimeResources, load_resources
from zonic_runtime.engine.agents.agent_factory import AgentServices
from zonic_runtime.engine.node_template import ServicePorts
from zonic_runtime.integrations.egress.guard import EgressGuard
from zonic_runtime.integrations.http.pools import BedrockClients, HttpPools
from zonic_runtime.integrations.mcp.execute import McpService
from zonic_runtime.integrations.mcp.pool import McpPool
from zonic_runtime.models.dialects.refs import ModelRef
from zonic_runtime.models.mcp import McpExecuteTool
from zonic_runtime.ports.mcp import RunMcp

pytestmark = pytest.mark.live

ENV = live_environment()
LIVE = load_resources(ENV)
BEDROCK_ON = os.environ.get("LIVE_BEDROCK") == "1"
OVERLAY = Path(__file__).resolve().parents[2] / "deploy" / "aws" / "resources" / "models.dev.json"
#: The models V-01 calls: every enabled one of a provider these tests reach.
CALLED = sorted(
    code
    for code, e in LIVE.models.models.items()
    if e.enabled and (e.provider == "openai" or (BEDROCK_ON and e.provider == "bedrock_converse"))
)


#: The model clients the running test opened; closed after it.
_POOLS: list[HttpPools] = []


@pytest.fixture(autouse=True)
async def _close_model_clients() -> AsyncIterator[None]:
    yield
    while _POOLS:
        await _POOLS.pop().aclose()


def _read(field: str) -> str | None:
    value = getattr(ENV, field, None)
    return value.get_secret_value() if isinstance(value, SecretStr) else value


def models(resources: RuntimeResources) -> AgentServices:
    """The real model services: OpenAI with its key; Bedrock with the AWS credentials."""
    if BEDROCK_ON:
        import boto3

        if boto3.Session().get_credentials() is None:
            pytest.skip("no AWS credentials (set AWS_PROFILE)")
    elif not _read("openai_api_key"):
        pytest.skip("ZONIC_OPENAI_API_KEY is not set")
    pools = HttpPools()  # this test's own clients, on its own event loop
    _POOLS.append(pools)
    return AgentServices.from_resources(
        resources,
        environment=ENV,
        bedrock=BedrockClients(resources.settings.models),
        http=lambda upstream: pools.get(upstream, httpx.AsyncClient),
    )


def standins(resources: RuntimeResources) -> RuntimeResources:
    """The deployment's model substitutes (Claude codes served by OpenAI), unless ``LIVE_BEDROCK``."""
    if BEDROCK_ON:
        return resources
    substitutes = read_json(OVERLAY)["substitutes"]
    return replace(resources, models=resources.models.model_copy(update={"substitutes": substitutes}))


def mcp(resources: RuntimeResources) -> McpService:
    if not ENV.mcp_base_url:
        pytest.skip("ZONIC_MCP_BASE_URL is not set")
    return McpService(McpPool(resources.mcp_servers, _read))


# --- V-01 ---------------------------------------------------------------------------------------


async def test_V01_each_catalog_model_answers_a_short_prompt() -> None:
    """One test for every model: the OpenAI client is cached per process, bound to one event loop."""
    agents = models(LIVE)
    assert CALLED
    for code in CALLED:
        model = agents.models.create(ModelRef(model_id=code), max_tokens=16, stream=False)
        reply = await model.ainvoke("Reply with the single word OK.")
        assert "OK" in str(reply.content).upper(), (code, reply.content)
        usage = reply.usage_metadata
        assert usage is not None and usage["input_tokens"] > 0 and usage["output_tokens"] > 0, code


# --- V-02 ---------------------------------------------------------------------------------------


@pytest.fixture
async def service() -> AsyncIterator[McpService]:
    found = mcp(LIVE)
    yield found
    await found.aclose()


def _v02_tools() -> dict[str, McpExecuteTool]:
    health = f"{(ENV.mcp_base_url or '').rstrip('/')}/health"
    return {
        "function": McpExecuteTool(
            id="fn",
            type="function",
            config={"functionPath": "statistics.mean", "callStyle": "kwargs"},
            input={"data": [2, 4]},
        ),
        "database": McpExecuteTool(
            id="db",
            type="database",
            config={
                "connectionName": "sample_open_data",
                "queryType": "script",
                "script": "SELECT count(*) AS tables FROM information_schema.tables"
                " WHERE table_schema = 'sample_open_data'",
            },
        ),
        "api": McpExecuteTool(id="api", type="api", config={"url": health, "method": "GET"}),
    }


@pytest.mark.parametrize(
    ("kind", "expected"), [("function", '"value": 3'), ("database", "tables"), ("api", "execute")]
)
async def test_V02_the_real_mcp_server_runs_an_execute_of_each_tool_type(
    service: McpService, kind: str, expected: str
) -> None:
    assert "execute" in {t.name for t in await service.tools()}
    tool = _v02_tools()[kind]
    result = await RunMcp(service, f"live-v02-{kind}", OWNER).execute(f"v02:{kind}", [tool])
    answer = result.results[tool.id]
    assert answer.status == "success", answer.error
    assert expected in json.dumps(answer.output), answer.output


# --- V-04 ---------------------------------------------------------------------------------------


async def test_V04_the_customer_support_swarm_hands_off_on_a_real_model() -> None:
    app = make_app(workflows={SUPPORT: support()}, agents=models(standins(DEV)), resources=standins(DEV))
    async with app.client() as client:
        body = {
            "agenticWorkflowId": SUPPORT,
            "input": {
                "text": "I was charged twice for invoice 42 this month. Can billing refund one charge?"
            },
            "mode": "stream",
        }
        response = await client.post("/v1/runs", json=body, headers=auth())
    events = sse_events(response.text)
    assert events[-1]["type"] == "run.completed", events[-1]
    result = events[-1]["data"]["result"]
    handoffs = [e["data"] for e in events if e["type"] == "swarm.handoff"]
    assert handoffs and result["history"][0] == "triage" and len(result["history"]) >= 2, result
    assert result["finalAgent"] == result["history"][-1] and result["answer"].strip()
    assert any(e["type"] == "token.delta" for e in events)


# --- V-05 ---------------------------------------------------------------------------------------


async def test_V05_the_energy_workflow_reads_real_open_data_through_mcp() -> None:
    """fsh-energy-agent on the live models and the real MCP server: the run completes, every node
    that ran has one observation and model nodes have usage; database tools that run query
    ``sample_open_data`` without an error."""
    resources = standins(DEV)
    service = mcp(resources)
    document = read_json(BUILDER / "open-data" / "fsh-energy-agent.json")
    app = make_app(
        workflows={"wf-energy": document},
        agents=models(resources),
        resources=resources,
        tracing=True,
        ports=ServicePorts(egress=EgressGuard(resources.egress), mcp=service),
    )
    assert app.deps.tracing is not None and app.obs is not None
    app.deps.tracing.start()
    try:
        async with app.client() as client:
            body = {
                "agenticWorkflowId": "wf-energy",
                "input": {
                    "text": "Look up the utility bills of user 1809 from 2024-01-01 to 2024-03-31 with the "
                    "utility bills tool, then summarise the total expense."
                },
                "mode": "stream",
            }
            response = await client.post("/v1/runs", json=body, headers=auth())
        await app.deps.tracing.flush()
    finally:
        await app.deps.tracing.aclose()
        await service.aclose()
    events = sse_events(response.text)
    assert events[-1]["type"] == "run.completed", events[-1]["data"]
    tools = [e["data"] for e in events if e["type"] == "tool.finished"]
    database = [t for t in tools if str(t.get("name", "")).startswith("get_utility_bills")]
    # The energy agent hands off to the data branch when its model calls the handoff tool: Claude
    # does; OpenAI's stand-in may answer with the handoff described in JSON instead. When the data
    # branch runs, its database tools read the real open data without an error.
    assert all(t.get("status") != "error" for t in database), tools
    ran = {e["nodeId"] for e in events if e["type"] == "node.finished"}
    observations = list(app.obs.observations.values())
    metadata = [o.get("metadata") or {} for o in observations]
    observed = {m.get("langgraph_node") or m.get("nodeId") for m in metadata}
    assert ran <= observed, ran - observed
    calls = [o for o in observations if o.get("type") == "generation" and o.get("model")]  # model calls
    assert calls and all((o.get("usageDetails") or {}).get("output", 0) > 0 for o in calls), calls


# --- F-14 ---------------------------------------------------------------------------------------


async def test_F14_the_live_facility_agent_answers_from_open_data_on_the_deployed_gateway() -> None:
    """fsh-facility-agent from the live catalog, on the live models and the deployed gateway: the agent's
    run succeeds with its Output's result, and every node it ran is recorded on the run."""

    def ports(found: RuntimeResources) -> Any:
        return ServicePorts(egress=EgressGuard(found.egress), mcp=mcp(found))

    async with open_live(
        [], "fsh-facility-agent", ports=ports, agents=models, substitutes=not BEDROCK_ON
    ) as app:
        response = await app.start("Which facilities are over budget?", mode="stream")
        events = sse_events(response.text)
        run_id = response.headers["x-run-id"]
        nodes = (await app.get(f"/v1/runs/{run_id}/nodes")).json()["items"]
        run = (await app.get(f"/v1/runs/{run_id}")).json()
    assert events[-1]["type"] == "run.completed", events[-1]
    assert run["status"] == "succeeded" and run["result"], run
    assert len(nodes) >= 3 and all(n["status"] == "succeeded" for n in nodes), nodes
    assert any(e["type"] == "token.delta" for e in events)
    assert OWNER and TENANT  # the rows are the live owner's (cleaned up after P5-03)
