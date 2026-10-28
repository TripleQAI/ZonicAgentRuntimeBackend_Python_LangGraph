"""The ``tool``, ``external_agent`` and ``guardrail`` kinds (P3-06: N-10, N-15, N-16, B-05), the
policy model of the ``guardrail`` hook, ``mcp-resource`` tools, and the platform scenarios'
cases (F-17, F-18, B-12, B-13)."""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import replace
from typing import Any

import pytest
from langchain.agents.structured_output import StructuredOutputValidationError

from tests.conformance.test_kinds import llm
from tests.fakes.egress import FakeEgress
from tests.fakes.mcp_server import FakeMcpServer, ServerToolsMcp
from tests.fakes.scripted_chat_model import ScriptedChatModel, Turn
from tests.kinds.agents import ScriptedModels
from tests.kinds.harness import DEFAULT_INPUT, Harness
from tests.scenarios.builder.scenarios import PLATFORM, SUB_AGENTS
from tests.scenarios.builder.test_builder_scenarios import run_turns
from tests.unit.test_hooks import DEV, binding
from tests.unit.test_integration_hooks import echo_harness, hooks_with
from zonic_runtime.engine.agents.agent_factory import AgentServices
from zonic_runtime.engine.agents.errors import provider_errors
from zonic_runtime.engine.agents.policy_guard import PolicyGuard
from zonic_runtime.engine.context import RunPorts
from zonic_runtime.engine.kinds.core import core_kinds
from zonic_runtime.engine.kinds.integration_kinds import a2a_text, tool_name_of
from zonic_runtime.engine.node_template import ServicePorts
from zonic_runtime.errors import ZonicError
from zonic_runtime.integrations.guardrails.bedrock import BedrockGuardrails
from zonic_runtime.integrations.mcp.execute import McpService
from zonic_runtime.ports.egress import EgressRequest, EgressResponse
from zonic_runtime.ports.guardrail import AppliedGuardrail
from zonic_runtime.ports.mcp import RunMcp

ENDPOINT = "https://httpbin.org/agent"


def services(
    script: Sequence[Any] = (),
    *,
    answers: dict[str, Any] | None = None,
    errors: dict[str, str] | None = None,
    egress: Any = None,
    guardrails: Any = None,
) -> tuple[Harness, ScriptedModels, ServerToolsMcp]:
    models = ScriptedModels(ScriptedChatModel(script=list(script)))
    agents = AgentServices.from_resources(DEV, builders=models.builders())
    mcp = ServerToolsMcp(McpService(FakeMcpServer().pool()), answers or {}, errors)
    ports = ServicePorts(mcp=mcp, egress=egress, guardrails=guardrails)
    harness = Harness(
        DEV,
        core_kinds(),
        agents=agents,
        ports=ports,
        run_ports=lambda run_id: RunPorts(mcp=RunMcp(mcp, run_id, "owner")),
    )
    return harness, models, mcp


def document(node: dict[str, Any], *, branches: bool = False) -> dict[str, Any]:
    nodes = [{"id": "start", "type": "start", "config": {}}, node]
    edges = [{"source": "start", "target": node["id"]}]
    if branches:
        nodes += [
            {"id": "ok", "type": "variable", "name": "ok", "config": {}},
            {"id": "bad", "type": "variable", "name": "bad", "config": {}},
        ]
        edges += [
            {"source": node["id"], "target": "ok", "sourceHandle": "pass"},
            {"source": node["id"], "target": "bad", "sourceHandle": "fail"},
        ]
    return {"schemaVersion": "2.0", "nodes": nodes, "edges": edges}


def started(events: list[dict[str, Any]]) -> list[str]:
    return [e["nodeId"] for e in events if e.get("event") == "node.started"]


def flow(**values: Any) -> dict[str, Any]:
    return {**DEFAULT_INPUT, "flow": {"values": values, "provenance": {}, "conflicts": []}}


# --- N-15, B-05 ---------------------------------------------------------------------------------


def tool_node(**config: Any) -> dict[str, Any]:
    base = {
        "toolType": "mcp",
        "toolId": "mcp-srv-lookup_site",
        "serverId": "srv",
        "parameters": {"site": "{{flow.site}}", "limit": 5},
        "inputs": [{"key": "region", "value": "east"}],
    }
    return {"id": "t", "type": "tool", "name": "lookup", "config": {**base, **config}}


async def test_N15_a_tool_node_calls_its_tool_with_resolved_parameters_and_no_model() -> None:
    h, models, mcp = services(answers={"lookup_site": {"site": "Main", "kwh": 410}})
    final, *_ = await h.run(document(tool_node()), flow(site="Main"))
    assert mcp.calls == [("srv", "lookup_site", {"site": "Main", "limit": 5, "region": "east"})]
    assert final["nodes"]["t"]["output"] == {"site": "Main", "kwh": 410}
    assert models.model.requests == []  # no model call


def test_N15_the_mcp_name_drops_the_platform_prefix() -> None:
    config = type("C", (), {"tool_name": None, "tool_id": "mcp-srv-lookup_site", "server_id": "srv"})()
    assert tool_name_of(config, "fallback") == "lookup_site"
    named = type("C", (), {"tool_name": "given", "tool_id": "x", "server_id": "srv"})()
    assert tool_name_of(named, "fallback") == "given"


async def test_B05_success_follows_pass_and_a_tool_error_follows_fail() -> None:
    h, *_ = services(answers={"lookup_site": {"ok": True}})
    _, events, *_ = await h.run(document(tool_node(), branches=True), flow(site="Main"))
    assert started(events) == ["start", "t", "ok"]
    h, *_ = services(errors={"lookup_site": "site not found"})
    final, events, *_ = await h.run(document(tool_node(), branches=True), flow(site="Main"))
    assert started(events) == ["start", "t", "bad"]
    assert "site not found" in final["nodes"]["t"]["output"]["error"]


async def test_B05_a_tool_error_without_a_fail_edge_fails_the_node() -> None:
    h, *_ = services(errors={"lookup_site": "site not found"})
    with pytest.raises(ZonicError, match="TOOL_FAILED"):
        await h.run(document(tool_node()), flow(site="Main"))


# --- N-16 ---------------------------------------------------------------------------------------


def agent_node(**config: Any) -> dict[str, Any]:
    base = {
        "endpoint": ENDPOINT,
        "inputMapping": [{"key": "question", "value": "{{system.userQuery}}"}],
        "retries": 2,
    }
    return {"id": "x", "type": "external_agent", "name": "x", "config": {**base, **config}}


def a2a_answer(text: str) -> bytes:
    return json.dumps(
        {
            "jsonrpc": "2.0",
            "id": "1",
            "result": {"kind": "message", "parts": [{"kind": "text", "text": text}]},
        }
    ).encode()


async def test_N16_an_a2a_agent_answers_through_the_egress_guard_after_a_retry() -> None:
    attempts: list[EgressRequest] = []

    def answer(request: EgressRequest) -> EgressResponse:
        attempts.append(request)
        if len(attempts) == 1:
            raise ZonicError("UPSTREAM_5XX", status=503)
        return EgressResponse(status=200, content=a2a_answer('{"answer": 42}'), url=request.url)

    h, *_ = services(egress=FakeEgress(DEV.egress, answer=answer))
    final, *_ = await h.run(document(agent_node()))
    out = final["nodes"]["x"]["output"]
    assert out["text"] == '{"answer": 42}' and out["result"] == {"answer": 42}
    assert len(attempts) == 2
    body = json.loads(attempts[-1].body)
    assert body["method"] == "message/send" and body["params"]["message"]["parts"][0]["text"] == "hello"


async def test_N16_http_protocol_posts_the_input_and_errors_are_external_agent_error() -> None:
    seen: list[EgressRequest] = []

    def answer(request: EgressRequest) -> EgressResponse:
        seen.append(request)
        return EgressResponse(status=200, content=b'{"ok": true}', url=request.url)

    h, *_ = services(egress=FakeEgress(DEV.egress, answer=answer))
    final, *_ = await h.run(document(agent_node(protocol="http")))
    assert json.loads(seen[0].body) == {"question": "hello"} and final["nodes"]["x"]["output"]["result"] == {
        "ok": True
    }

    def down(request: EgressRequest) -> EgressResponse:
        raise ZonicError("UPSTREAM_5XX", status=502)

    h, *_ = services(egress=FakeEgress(DEV.egress, answer=down))
    with pytest.raises(ZonicError, match="EXTERNAL_AGENT_ERROR"):
        await h.run(document(agent_node(retries=1)))


def test_N16_a2a_text_reads_messages_artifacts_and_status() -> None:
    assert (
        a2a_text({"parts": [{"kind": "text", "text": "a"}, {"kind": "data", "data": {"b": 1}}]})
        == 'a\n{"b": 1}'
    )
    assert a2a_text({"artifacts": [{"parts": [{"text": "c"}]}]}) == "c"
    assert a2a_text({"status": {"message": {"parts": [{"text": "d"}]}}}) == "d"


# --- N-10 ---------------------------------------------------------------------------------------


def guardrail_node(**config: Any) -> dict[str, Any]:
    base = {"inputMapping": [{"key": "text", "value": "{{system.userQuery}}"}], "policy": "No personal data."}
    return {"id": "g", "type": "guardrail", "name": "g", "config": {**base, **config}}


def decision(allowed: bool, reason: str = "") -> Turn:
    return Turn.structured(
        "GuardrailDecision", {"allowed": allowed, "reason": reason, "categories": [] if allowed else ["pii"]}
    )


async def test_N10_compliant_content_passes_and_flagged_content_fails() -> None:
    h, models, _ = services([decision(True)])
    final, events, *_ = await h.run(document(guardrail_node(), branches=True))
    assert started(events) == ["start", "g", "ok"] and final["nodes"]["g"]["output"]["passed"] is True
    prompt = models.model.requests[0].messages[0].text
    assert "No personal data." in prompt  # settings.guardrails.prompt with the node's policy
    h, *_ = services([decision(False, "an email address")])
    final, events, *_ = await h.run(document(guardrail_node(), branches=True))
    assert started(events) == ["start", "g", "bad"]
    assert final["nodes"]["g"]["output"]["findings"] == [
        {"reason": "an email address", "categories": ["pii"]}
    ]


async def test_N10_warn_passes_with_a_warning_and_no_fail_edge_blocks() -> None:
    h, *_ = services([decision(False, "an email address")])
    _, events, *_ = await h.run(document(guardrail_node(action="warn"), branches=True))
    assert started(events) == ["start", "g", "ok"]
    assert any(e.get("event") == "warning" and e.get("code") == "guardrail.flagged" for e in events)
    h, *_ = services([decision(False, "an email address")])
    with pytest.raises(ZonicError, match="GUARDRAIL_BLOCKED"):
        await h.run(document(guardrail_node()))


class FakeManaged:
    def __init__(self, intervened: bool) -> None:
        self.intervened = intervened
        self.calls: list[tuple[str, str, str, str]] = []

    async def apply(self, guardrail_id: str, version: str, source: str, text: str) -> AppliedGuardrail:
        self.calls.append((guardrail_id, version, source, text))
        return AppliedGuardrail(
            intervened=self.intervened,
            reason="PII" if self.intervened else "",
            categories=["EMAIL"] if self.intervened else [],
        )


async def test_N10_a_bedrock_guardrail_decides_without_a_model_call() -> None:
    managed = FakeManaged(intervened=True)
    h, models, _ = services(guardrails=managed)
    _, events, *_ = await h.run(document(guardrail_node(guardrailId="gr-1"), branches=True))
    assert started(events) == ["start", "g", "bad"] and models.model.requests == []
    assert managed.calls == [("gr-1", DEV.settings.guardrails.default_version, "INPUT", "hello")]
    h, *_ = services()
    with pytest.raises(ZonicError, match="FEATURE_DISABLED"):
        await h.run(document(guardrail_node(guardrailId="gr-1")))


async def test_the_bedrock_adapter_reads_interventions_and_categories() -> None:
    class Client:
        def apply_guardrail(self, **kwargs: Any) -> dict[str, Any]:
            assert (
                kwargs["content"] == [{"text": {"text": "mail me at a@b.co"}}]
                and kwargs["source"] == "OUTPUT"
            )
            return {
                "action": "GUARDRAIL_INTERVENED",
                "outputs": [{"text": "mail me at {EMAIL}"}],
                "assessments": [
                    {
                        "sensitiveInformationPolicy": {
                            "piiEntities": [{"type": "EMAIL", "action": "ANONYMIZED"}]
                        }
                    }
                ],
            }

    applied = await BedrockGuardrails("eu-west-2", lambda _: Client()).apply(
        "gr-1", "1", "OUTPUT", "mail me at a@b.co"
    )
    assert applied.intervened and applied.categories == ["EMAIL"] and applied.text == "mail me at {EMAIL}"


# --- the policy model of the guardrail hook (P3-10's policy_model provider) -----------------------


async def test_the_guardrail_hook_asks_the_policy_model() -> None:
    models = ScriptedModels(ScriptedChatModel(script=[decision(False, "a phone number")]))
    agents = AgentServices.from_resources(DEV, builders=models.builders())
    guard = PolicyGuard(
        DEV.settings.guardrails.model_copy(update={"policies": {"pii": "No phone numbers."}}), agents.models
    )
    settings = DEV.settings.model_copy(
        update={
            "guardrails": DEV.settings.guardrails.model_copy(
                update={"policies": {"pii": "No phone numbers."}}
            )
        }
    )

    resources = replace(DEV, settings=settings)
    config = {"provider": "policy_model", "policyRef": "pii", "action": "block"}
    hooks = hooks_with(
        [binding("safe", "afterNode", hook="guardrail", match={"nodeNames": ["a"]}, config=config)],
        resources,
        guard=guard,
    )

    h = echo_harness(hooks, resources, text="call 555 0100")
    with pytest.raises(ZonicError, match="GUARDRAIL_BLOCKED"):
        await h.run(
            {
                "schemaVersion": "2.0",
                "nodes": [{"id": "start", "type": "start", "config": {}}, llm("a")],
                "edges": [{"source": "start", "target": "a"}],
            }
        )
    assert "No phone numbers." in models.model.requests[0].messages[0].text


async def test_a_native_structured_answer_that_is_not_json_is_structured_output_invalid() -> None:
    with pytest.raises(ZonicError, match="STRUCTURED_OUTPUT_INVALID"):
        async with provider_errors("n"):
            raise StructuredOutputValidationError("response_format", ValueError("not JSON"), None)


# --- mcp-resource ---------------------------------------------------------------------------------


async def test_an_mcp_resource_tool_reads_the_servers_resource() -> None:
    from zonic_runtime.models.dialects.refs import ToolRef

    h, _, mcp = services()
    tool = h.agents.tools.create(
        h.plan(document(tool_node())).nodes["t"],
        ToolRef(
            tool_type="mcp-resource",
            name="kb.xlsx",
            tool_id="file-1",
            server_id="srv",
            settings={"settings": {"uri": "file://kb.xlsx"}},
        ),
    )
    from langchain.tools import ToolRuntime

    context = h.context(h.plan(document(tool_node())))
    runtime = ToolRuntime(
        state={}, context=context, config={}, stream_writer=lambda _: None, tool_call_id="c1", store=None
    )
    assert await tool.coroutine(runtime) == "resource file://kb.xlsx"  # type: ignore[attr-defined]
    assert mcp.calls == [("srv", "resource:file://kb.xlsx", {})]


# --- F-17, B-12, F-18, B-13: the platform workflows ------------------------------------------------


async def orchestrator_turns() -> list[dict[str, Any]]:
    [scenario] = [s for s in PLATFORM if s.name == "platform-intelligent-buying-orchestrator"]
    return await run_turns(scenario, "stream")


async def test_F17_the_orchestrator_routes_on_current_state_across_turns() -> None:
    first, second = await orchestrator_turns()
    assert "quick_pay" in first["path"] and "get_user_context" in first["path"]
    # The saved current_state locks the channel: straight to quick pay, no context agent.
    assert (
        second["path"][:3] == ["start", "channel_lockin", "quick_pay"]
        and "get_user_context" not in second["path"]
    )


async def test_B12_agent_json_runs_end_to_end_over_two_turns_into_its_sub_flow() -> None:
    first, second = await orchestrator_turns()
    for turn in (first, second):
        assert turn["path"][-1] == "output" and turn["events"][-1]["type"] == "run.completed"
        nested = [e for e in turn["events"] if e["type"] == "node.finished" and e.get("path")]
        assert nested and all(e["data"]["status"] == "succeeded" for e in nested)


async def sub_agent_run(name: str) -> tuple[list[str], list[dict[str, Any]], tuple[str, ...]]:
    [scenario] = [s for s in PLATFORM if s.name == f"platform-{name}"]
    [turn] = scenario.turns
    [found] = await run_turns(scenario, "stream")
    return found["path"], found["events"], turn.path


@pytest.mark.parametrize("name", list(SUB_AGENTS))
async def test_F18_each_sub_agent_runs_its_agent_and_tool_loop_twice_then_answers(name: str) -> None:
    path, _, expected = await sub_agent_run(name)
    assert path == list(expected) and path[-1] == "output"
    loop = path[len(SUB_AGENTS[name]) : -1]
    agent = loop[0]
    assert loop.count(agent) == 3 and loop[-1] == agent


@pytest.mark.parametrize("name", list(SUB_AGENTS))
async def test_B13_the_loop_nodes_run_twice_in_order_without_failures(name: str) -> None:
    path, events, _ = await sub_agent_run(name)
    loop = path[len(SUB_AGENTS[name]) : -1]
    agent = loop[0]
    at = [i for i, node in enumerate(loop) if node == agent]
    assert len(at) == 3
    first, second = loop[at[0] + 1 : at[1]], loop[at[1] + 1 : at[2]]
    assert first and first == second  # the same tool and variable nodes, in the same order
    assert not [e for e in events if e["type"] == "node.failed"]
