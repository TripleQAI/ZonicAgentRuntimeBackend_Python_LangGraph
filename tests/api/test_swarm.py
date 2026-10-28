"""The Swarm node (P4-01: G-06, W-01 to W-13, K-13, L-06), through the app and the harness.

``customer_support_swarm`` (members triage, billing and tech_support) for the template cases,
and small documents built here for an LLM member, a Rule after the Swarm, a Swarm inside a
sub-flow and two Swarms in one workflow.
"""

from __future__ import annotations

import asyncio
import copy
from typing import Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import tool
from langgraph.types import Command, interrupt

from tests.api.app import DEV, auth, make_app, sse_events
from tests.fakes.scripted_chat_model import Turn
from tests.kinds.agents import agent_harness
from tests.kinds.harness import with_checkpointer
from tests.unit.test_hooks import binding, template_hooks
from tests.workflow_fixtures import BUILDER, read_json
from zonic_runtime.engine.agents.swarm.handoff_tools import SwarmLimits
from zonic_runtime.engine.kinds.swarm_kind import MultiAgentKind
from zonic_runtime.errors import ZonicError

SUPPORT, TEAM, PARENT = "wf-support", "wf-team", "wf-parent"
QUESTION = "Was invoice 42 paid?"
TEMPLATE = read_json(BUILDER / "templates" / "customer_support_swarm.json")
RESULT = [
    {"target": "answer", "source": "{{nodes.support_team.output.text}}", "type": "string"},
    {"target": "finalAgent", "source": "{{nodes.support_team.output.finalAgent}}", "type": "string"},
    {"target": "history", "source": "{{nodes.support_team.output.nodeHistory}}", "type": "array"},
]


def support(**config: Any) -> dict[str, Any]:
    """The template with its Swarm's configuration changed (``None`` removes a key) and an Output
    that also returns ``finalAgent`` and ``nodeHistory``."""
    found = copy.deepcopy(TEMPLATE)
    nodes = {n["id"]: n for n in found["document"]["nodes"]}
    swarm = nodes["swarm_1"]["config"]
    swarm.update(config)
    for key in [k for k, v in swarm.items() if v is None]:
        del swarm[key]
    nodes["output_1"]["config"]["outputMapping"] = RESULT
    return found


def transfer(to: str, message: str = "Over to you.", **kwargs: Any) -> Turn:
    return Turn.call(f"transfer_to_{to}", {"message": message}, **kwargs)


def member(node_id: str, team: str = "team", type_: str = "agent", **config: Any) -> dict[str, Any]:
    prompt = f"You are {node_id}."
    own = (
        {"systemPrompt": prompt}
        if type_ == "agent"
        else {"messages": [{"role": "system", "content": prompt}]}
    )
    return {
        "id": node_id,
        "type": type_,
        "name": node_id,
        "description": f"The {node_id} member",
        "parentId": team,
        "config": {**own, **config},
    }


def swarm(node_id: str, members: list[dict[str, Any]], **config: Any) -> dict[str, Any]:
    return {
        "id": node_id,
        "type": "swarm",
        "name": node_id,
        "config": {"members": [m["id"] for m in members], **config},
    }


def output(node_id: str = "out", **mapping: str) -> dict[str, Any]:
    entries = [
        {"target": k, "source": v, "type": "array" if "nodeHistory" in v else "string"}
        for k, v in mapping.items()
    ]
    return {"id": node_id, "type": "output", "name": node_id, "config": {"outputMapping": entries}}


def document(nodes: list[dict[str, Any]], edges: list[tuple[str, ...]], **extra: Any) -> dict[str, Any]:
    links = [
        {"source": e[0], "target": e[1], **({"sourceHandle": e[2]} if len(e) > 2 else {})} for e in edges
    ]
    start = {"id": "start", "type": "start", "name": "Start", "config": {}}
    return {"schemaVersion": "2.0", "nodes": [start, *nodes], "edges": links, **extra}


def team(*members: dict[str, Any], **config: Any) -> dict[str, Any]:
    """Start -> team (the members) -> out, returning the Swarm's answer, final agent and history."""
    result = output(
        answer="{{nodes.team.output.text}}",
        finalAgent="{{nodes.team.output.finalAgent}}",
        history="{{nodes.team.output.nodeHistory}}",
    )
    return document(
        [swarm("team", list(members), **config), *members, result], [("start", "team"), ("team", "out")]
    )


async def run(
    workflows: dict[str, Any],
    script: list[Any],
    *,
    workflow: str = SUPPORT,
    mode: str = "sync",
    **settings: Any,
) -> tuple[Any, Any]:
    app = make_app(script, workflows=workflows, **settings)
    async with app.client() as client:
        body = {"agenticWorkflowId": workflow, "input": {"text": QUESTION}, "mode": mode}
        response = await client.post("/v1/runs", json=body, headers=auth())
    return app, response


def of(events: list[dict[str, Any]], *types: str) -> list[dict[str, Any]]:
    return [e for e in events if e["type"] in types]


def failed(response: Any) -> str:
    body = response.json()
    assert body["status"] == "failed", body
    return str(body["error"]["code"])


# --- G-06 --------------------------------------------------------------------------------------


async def test_G06_a_swarm_is_a_subgraph_with_a_node_per_member_and_transfers_to_every_other_member() -> None:
    kinds: list[MultiAgentKind] = []

    def capture(spec: Any, blueprint: Any) -> MultiAgentKind:
        kinds.append(MultiAgentKind(spec, blueprint))
        return kinds[-1]

    script = [transfer("billing"), transfer("tech_support"), Turn.say("Restart the router.")]
    h, models = agent_harness(script, DEV, kinds={"multi_agent": capture})
    final, *_ = await h.run(support()["document"])
    [kind] = kinds
    members = {s.graph_name for s in kind.member_specs}
    assert set(kind._graph.get_graph().nodes) - {"__start__", "__end__"} == members and len(members) == 3
    offered = [sorted(r.tool_names) for r in models.model.requests]
    assert offered == [
        ["transfer_to_billing", "transfer_to_tech_support"],
        ["transfer_to_tech_support", "transfer_to_triage"],
        ["transfer_to_billing", "transfer_to_triage"],
    ]
    assert final["nodes"]["swarm_1"]["output"]["nodeHistory"] == ["triage", "billing", "tech_support"]


# --- W-01, W-02, W-03 --------------------------------------------------------------------------


async def test_W01_a_single_member_answers_and_is_the_final_agent() -> None:
    _, response = await run({SUPPORT: support()}, [Turn.say("Hello! How can I help?")])
    body = response.json()
    assert body["status"] == "succeeded"
    assert body["result"] == {
        "answer": "Hello! How can I help?",
        "finalAgent": "triage",
        "history": ["triage"],
    }


async def test_W02_a_handoff_chain_is_reported_in_order_with_a_valid_history() -> None:
    script = [transfer("billing", "Invoice 42.", text="Billing it is."), Turn.say("Invoice 42 was paid.")]
    app, response = await run({SUPPORT: support()}, script, mode="stream")
    events = sse_events(response.text)
    swarm_events = [
        (e["type"], e["data"].get("member") or e["data"].get("from"))
        for e in of(events, "swarm.member.started", "swarm.member.finished", "swarm.handoff")
    ]
    assert swarm_events == [
        ("swarm.member.started", "triage"),
        ("swarm.member.finished", "triage"),
        ("swarm.handoff", "triage"),
        ("swarm.member.started", "billing"),
        ("swarm.member.finished", "billing"),
    ]
    [handoff] = of(events, "swarm.handoff")
    assert handoff["data"] == {"from": "triage", "to": ["billing"], "message": "Invoice 42."}
    assert all(e["nodeId"] == "swarm_1" and "path" not in e for e in of(events, "swarm.handoff"))
    assert events[-1]["data"]["result"]["history"] == ["triage", "billing"]
    # Every tool call billing's model sees is answered by its acknowledgement, next.
    messages = app.models.model.requests[1].messages
    for i, message in enumerate(messages):
        if isinstance(message, AIMessage) and message.tool_calls:
            [call] = message.tool_calls
            reply = messages[i + 1]
            assert isinstance(reply, ToolMessage) and reply.tool_call_id == call["id"]


@pytest.mark.parametrize(("entry", "first"), [("", "triage"), ("agent_billing", "billing")])
async def test_W03_the_entry_point_is_the_first_member_unless_set(entry: str, first: str) -> None:
    _, response = await run({SUPPORT: support(entryPoint=entry)}, [Turn.say("Hi.")])
    assert response.json()["result"]["finalAgent"] == first


# --- W-04, W-05, W-06 --------------------------------------------------------------------------


async def test_W04_a_handoff_past_max_handoffs_is_refused_with_swarm_step_limit() -> None:
    script = [transfer("billing"), transfer("tech_support")]
    app, response = await run({SUPPORT: support(maxHandoffs=1, maxIterations=15)}, script)
    assert failed(response) == "SWARM_STEP_LIMIT" and len(app.models.model.requests) == 2


async def test_W04_a_lower_max_iterations_is_the_limit() -> None:
    app, response = await run({SUPPORT: support(maxHandoffs=10, maxIterations=1)}, [transfer("billing")])
    assert failed(response) == "SWARM_STEP_LIMIT" and len(app.models.model.requests) == 1


async def test_W05_the_swarm_past_its_execution_timeout_is_swarm_timeout() -> None:
    config = support(executionTimeoutSeconds=0.2, nodeTimeoutSeconds=30)
    _, response = await run({SUPPORT: config}, [Turn.say("Slow.", delay=3)])
    assert failed(response) == "SWARM_TIMEOUT"


async def test_W05_a_member_past_its_node_timeout_fails_the_swarm_with_node_timeout() -> None:
    config = support(executionTimeoutSeconds=30, nodeTimeoutSeconds=0.2)
    _, response = await run(
        {SUPPORT: config}, [transfer("billing"), Turn.say("Slow.", delay=3)], mode="stream"
    )
    events = sse_events(response.text)
    assert events[-1]["type"] == "run.failed" and events[-1]["data"]["code"] == "NODE_TIMEOUT"
    assert [e["data"]["status"] for e in of(events, "swarm.member.finished")] == ["completed", "failed"]


async def test_W06_repetitive_handoffs_are_swarm_repetitive_handoff() -> None:
    config = support(repetitiveHandoffDetectionWindow=3, repetitiveHandoffMinUniqueAgents=3)
    script = [transfer("billing"), transfer("triage"), transfer("billing")]
    _, response = await run({SUPPORT: config}, script)
    assert failed(response) == "SWARM_REPETITIVE_HANDOFF"


async def test_W06_with_only_one_detection_setting_detection_is_off_with_a_warning() -> None:
    config = support(repetitiveHandoffDetectionWindow=3, repetitiveHandoffMinUniqueAgents=None)
    script = [transfer("billing"), transfer("triage"), transfer("billing"), Turn.say("Paid.")]
    _, response = await run({SUPPORT: config}, script, mode="stream")
    events = sse_events(response.text)
    assert events[-1]["type"] == "run.completed"
    assert [w["data"]["code"] for w in of(events, "warning")] == ["SWARM_DETECTION_OFF"]
    assert events[-1]["data"]["result"]["history"] == ["triage", "billing", "triage", "billing"]


# --- W-07 --------------------------------------------------------------------------------------


async def test_W07_in_shared_mode_the_next_member_gets_the_task_and_the_handoffs() -> None:
    app, _ = await run({SUPPORT: support()}, [transfer("billing", "Invoice 42."), Turn.say("Paid.")])
    system, preamble, call, ack = app.models.model.requests[1].messages
    assert isinstance(system, SystemMessage) and isinstance(preamble, HumanMessage)
    assert preamble.text.splitlines() == [
        f"Original task: {QUESTION}",
        "Previous agents: triage -> billing",
        "Available agents: triage (Classifies the request and hands it to the right specialist);"
        " tech_support (Troubleshoots product and integration issues)",
        "Handoff message: Invoice 42.",
    ]
    assert (
        isinstance(call, AIMessage) and isinstance(ack, ToolMessage) and ack.text == "Transferred to billing."
    )


async def test_W07_in_handoff_mode_the_next_member_gets_only_the_handoff() -> None:
    app, _ = await run(
        {SUPPORT: support()},
        [transfer("billing", "Invoice 42."), Turn.say("Paid.")],
        swarm={"context_mode": "handoff"},
    )
    _, message, call, ack = app.models.model.requests[1].messages
    assert message.text == "Invoice 42." and QUESTION not in str(
        [m.content for m in app.models.model.requests[1].messages]
    )
    assert [c["name"] for c in call.tool_calls] == ["transfer_to_billing"] and isinstance(ack, ToolMessage)


# --- W-08 --------------------------------------------------------------------------------------


async def test_W08_a_members_tool_and_text_events_carry_the_member_and_usage_is_summed() -> None:
    script = [transfer("tech_support"), Turn.call("current_time", {}), Turn.say("It is noon.")]
    _, response = await run({SUPPORT: support()}, script, mode="stream")
    events = sse_events(response.text)
    tools = [(e["data"]["toolName"], e["data"]["member"]) for e in of(events, "tool.started")]
    assert tools == [("transfer_to_tech_support", "triage"), ("current_time", "tech_support")]
    assert all(e["data"]["member"] == "tech_support" for e in of(events, "tool.finished")[1:])
    assert {e["data"]["member"] for e in of(events, "token.delta")} == {"tech_support"}
    assert all(e["nodeId"] == "swarm_1" for e in of(events, "token.delta", "tool.started", "tool.finished"))
    members = sum(e["data"]["usage"]["modelCalls"] for e in of(events, "swarm.member.finished"))
    [swarm_finished] = [e for e in of(events, "node.finished") if e["nodeId"] == "swarm_1"]
    assert (
        members
        == swarm_finished["data"]["usage"]["modelCalls"]
        == events[-1]["data"]["usage"]["modelCalls"]
        == 3
    )


# --- W-09 --------------------------------------------------------------------------------------


async def test_W09_output_mapping_after_the_swarm_and_a_rule_on_its_final_agent() -> None:
    a, b = member("a"), member("b")
    rule = {
        "id": "route",
        "type": "rule",
        "name": "route",
        "config": {
            "rules": [
                {
                    "ruleId": "r_b",
                    "conditions": [{"field": "{{flow.lastAgent}}", "op": "eq", "value": "b"}],
                },
                {"ruleId": "r_else", "isDefault": True, "conditions": []},
            ]
        },
    }
    node = swarm(
        "team",
        [a, b],
        outputStructure=[{"target": "summary", "value": "{{nodeOutput.finalAgent}}: {{nodeOutput.text}}"}],
        variableUpdates=[
            {"path": "flow.lastAgent", "op": "set", "value": "{{nodes.team.output.finalAgent}}"}
        ],
    )
    doc = document(
        [
            node,
            a,
            b,
            rule,
            output("by_b", answer="{{nodes.team.output.summary}}"),
            output("other", answer="no"),
        ],
        [("start", "team"), ("team", "route"), ("route", "by_b", "r_b"), ("route", "other", "r_else")],
    )
    _, response = await run({TEAM: doc}, [transfer("b"), Turn.say("Done.")], workflow=TEAM, mode="stream")
    events = sse_events(response.text)
    assert [e["nodeId"] for e in of(events, "node.started")] == ["start", "team", "route", "by_b"]
    assert events[-1]["data"]["result"] == {"answer": "b: Done."}


# --- W-10 --------------------------------------------------------------------------------------


async def test_W10_cancelling_a_run_during_a_swarm_stops_its_members() -> None:
    app = make_app([Turn.say("Thinking very slowly.", delay=10)], workflows={SUPPORT: support()})
    async with app.client() as client:
        body = {"agenticWorkflowId": SUPPORT, "input": {"text": QUESTION}, "mode": "sync"}
        request = asyncio.create_task(client.post("/v1/runs", json=body, headers=auth()))
        for _ in range(200):
            if app.models.model.requests and app.deps.registry.runs():
                break
            await asyncio.sleep(0.01)
        [active] = app.deps.registry.runs()
        active.cancel()
        response = await asyncio.wait_for(request, 5)
    assert response.json()["status"] == "cancelled"
    assert len(app.models.model.requests) == 1  # no member after the cancelled one


# --- W-11 --------------------------------------------------------------------------------------


@tool
async def refund(amount: int) -> str:
    """Refund an amount once a person approves it."""
    approved = interrupt({"approve": amount})
    return f"refunded {amount}" if approved == "yes" else "not refunded"


async def test_W11_an_interrupt_in_a_member_pauses_and_resumes_that_member_without_repeating_its_calls() -> (
    None
):
    script = [transfer("billing"), Turn.call("refund", {"amount": 5}), Turn.say("Refunded 5.")]
    h, models = agent_harness(script, DEV, tools={"agent_billing": [refund]})
    h = with_checkpointer(h)
    blueprint = h.plan(support()["document"])
    graph = h.compile(blueprint)
    config, context = h.config(blueprint), h.context(blueprint)
    first = await graph.ainvoke(
        {"system": {"values": {"userQuery": QUESTION}}}, config=config, context=context
    )
    [pending] = first["__interrupt__"]
    assert pending.value == {"approve": 5} and len(models.model.requests) == 2
    final = await graph.ainvoke(Command(resume={pending.id: "yes"}), config=config, context=context)
    assert len(models.model.requests) == 3  # only the call after the tool's result
    last = models.model.requests[2].messages
    assert isinstance(last[-1], ToolMessage) and last[-1].text == "refunded 5"
    assert final["nodes"]["swarm_1"]["output"]["nodeHistory"] == ["triage", "billing"]


# --- W-12 --------------------------------------------------------------------------------------


async def test_W12_a_swarm_inside_a_sub_flow_reports_its_nested_path() -> None:
    from tests.fakes.subflows import subflow

    child = team(member("a"), member("b"))
    child["agenticWorkflowId"] = TEAM
    parent = document(
        [subflow("sub", TEAM, inputs=[]), output(answer="{{nodes.sub.output.result.answer}}")],
        [("start", "sub"), ("sub", "out")],
        agenticWorkflowId=PARENT,
    )
    script = [transfer("b"), Turn.say("From the child.")]
    _, response = await run({PARENT: parent, TEAM: child}, script, workflow=PARENT, mode="stream")
    events = sse_events(response.text)
    nested = of(events, "swarm.member.started", "swarm.handoff", "token.delta")
    assert nested and all(e["nodeId"] == "team" and e["path"] == "sub" for e in nested)
    assert events[-1]["data"]["result"] == {"answer": "From the child."}


async def test_W12_two_swarms_in_one_workflow_keep_their_own_state() -> None:
    one, two = [member("a1", "t1"), member("b1", "t1")], [member("a2", "t2"), member("b2", "t2")]
    doc = document(
        [
            swarm("t1", one),
            *one,
            swarm("t2", two),
            *two,
            output(first="{{nodes.t1.output.finalAgent}}", second="{{nodes.t2.output.finalAgent}}"),
        ],
        [("start", "t1"), ("t1", "t2"), ("t2", "out")],
    )
    script = [transfer("b1"), Turn.say("First."), Turn.say("Second.")]
    app, response = await run({TEAM: doc}, script, workflow=TEAM)
    assert response.json()["result"] == {"first": "b1", "second": "a2"}
    second = app.models.model.requests[2].messages
    assert [type(m) for m in second] == [SystemMessage, HumanMessage]  # nothing of the first Swarm


# --- W-13 --------------------------------------------------------------------------------------


async def test_W13_an_llm_member_works_as_an_agent_without_tools_of_its_own() -> None:
    writer = member("writer", type_="llm", tools=[{"name": "current_time", "type": "strands_builtin"}])
    doc = team(member("lead"), writer)
    app, response = await run(
        {TEAM: doc}, [transfer("writer", "Write it up."), Turn.say("# Report")], workflow=TEAM
    )
    assert response.json()["result"] == {
        "answer": "# Report",
        "finalAgent": "writer",
        "history": ["lead", "writer"],
    }
    assert app.models.model.requests[1].tool_names == ["transfer_to_lead"]


# --- K-13 --------------------------------------------------------------------------------------


async def test_K13_before_member_call_cancels_the_members_step() -> None:
    bindings = [
        binding(
            "closed",
            "beforeMemberCall",
            match={"nodeNames": ["billing"]},
            config={"action": "cancel", "message": "Billing is closed."},
        )
    ]
    h, models = agent_harness([transfer("billing"), Turn.say("never")], DEV)
    h.hooks = template_hooks(bindings)
    with pytest.raises(ZonicError) as caught:
        await h.run(support()["document"])
    assert caught.value.code == "SWARM_MEMBER_CANCELLED" and caught.value.details["member"] == "billing"
    assert len(models.model.requests) == 1  # billing's model is never called


# --- L-06 --------------------------------------------------------------------------------------


async def test_L06_member_steps_are_observations_under_the_swarm_with_handoff_events() -> None:
    export = DEV.settings.observability.export.model_copy(update={"retry_backoff_milliseconds": 5})
    script = [transfer("billing"), transfer("tech_support"), Turn.say("Restart it.")]
    app = make_app(script, workflows={SUPPORT: support()}, tracing=True, observability={"export": export})
    assert app.deps.tracing is not None and app.obs is not None
    app.deps.tracing.start()
    try:
        async with app.client() as client:
            body = {"agenticWorkflowId": SUPPORT, "input": {"text": QUESTION}, "mode": "sync"}
            await client.post("/v1/runs", json=body, headers=auth())
        await app.deps.tracing.flush()
    finally:
        await app.deps.tracing.aclose()
    found = list(app.obs.observations.values())
    by_id = {o["observationId"]: o for o in found}
    nodes = [o for o in found if o.get("visit")]  # node spans; spans inside a node carry no visit
    [team_span] = [o for o in nodes if o.get("nodeId") == "swarm_1"]

    def under(observation: dict[str, Any]) -> bool:
        current = observation.get("parentObservationId")
        while current is not None and current != team_span["observationId"]:
            current = by_id[current].get("parentObservationId")
        return current == team_span["observationId"]

    steps = [o for o in nodes if o.get("nodeType") == "agent"]
    assert sorted(o["nodeId"] for o in steps) == ["agent_billing", "agent_tech", "agent_triage"]
    assert all(under(o) for o in steps)
    handoffs = [o for o in found if o.get("name") == "swarm.handoff"]
    assert len(handoffs) == 2 and all(under(o) for o in handoffs)


# --- F-11, F-12 ----------------------------------------------------------------------------------

FACILITY = read_json(BUILDER / "templates" / "facility_operations_swarm.json")
FACILITY_MEMBERS = ("agent_coordinator", "agent_energy", "agent_facility", "agent_risk", "llm_writer")


def facility_team() -> dict[str, Any]:
    """The Facility Operations template's Start, its Swarm node and five members, then an Output:
    the rest of the template (the approval among it) is P4-02's."""
    nodes = {n["id"]: n for n in copy.deepcopy(FACILITY["document"]["nodes"])}
    result = output(
        report="{{nodes.operations_team.output.text}}",
        finalAgent="{{nodes.operations_team.output.finalAgent}}",
        history="{{nodes.operations_team.output.nodeHistory}}",
    )
    kept = [nodes["swarm_ops"], *(nodes[m] for m in FACILITY_MEMBERS), result]
    found = document(kept, [("start_1", "swarm_ops"), ("swarm_ops", "out")])
    found["nodes"][0] = nodes["start_1"]
    return found


def limits_of(raw: dict[str, Any], node_id: str) -> SwarmLimits:
    from tests.kinds.agents import agent_harness as harness

    h, _ = harness([], DEV)
    spec = h.plan(raw).nodes[node_id]
    return SwarmLimits.of(spec.config, DEV.settings.swarm, len(spec.config.members))[0]  # type: ignore[attr-defined]


async def test_F11_customer_support_triage_hands_off_to_billing_within_the_templates_limits() -> None:
    assert limits_of(TEMPLATE["document"], "swarm_1") == SwarmLimits(steps=11, window=6, unique=2)
    script = [
        transfer("billing", "Duplicate charge on invoice 7"),
        Turn.say("We refunded the duplicate charge."),
    ]
    _, response = await run({SUPPORT: support()}, script)
    assert response.json()["result"] == {
        "answer": "We refunded the duplicate charge.",
        "finalAgent": "billing",
        "history": ["triage", "billing"],
    }


async def test_F12_facility_operations_members_limits_and_timeouts() -> None:
    raw = facility_team()
    assert limits_of(raw, "swarm_ops") == SwarmLimits(steps=13, window=8, unique=3)
    kinds: list[MultiAgentKind] = []

    def capture(spec: Any, blueprint: Any) -> MultiAgentKind:
        kinds.append(MultiAgentKind(spec, blueprint))
        return kinds[-1]

    h, _ = agent_harness([Turn.say("# Report\nSEVERITY: 3")], DEV, kinds={"multi_agent": capture})
    final, *_ = await h.run(raw)
    [kind] = kinds
    assert kind.spec.timeout_seconds == 900 and kind.entry is not None and kind.entry.name == "coordinator"
    assert [s.node_type for s in kind.member_specs] == ["agent", "agent", "agent", "agent", "llm"]
    timeouts = {name: spec.timeout.run_timeout for name, spec in kind._graph.builder.nodes.items()}
    assert set(timeouts.values()) == {240}
    assert final["nodes"]["swarm_ops"]["output"]["finalAgent"] == "coordinator"


async def test_F12_facility_operations_members_run_with_their_tools_and_the_llm_writer() -> None:
    from tests.fakes.mcp_server import FakeMcpServer, ok
    from zonic_runtime.engine.node_template import ServicePorts
    from zonic_runtime.integrations.mcp.execute import McpService

    answers = {"get_utility_bills_with_historical_data": ok(None, [{"expense": 1200}])}
    ports = ServicePorts(mcp=McpService(FakeMcpServer(answers=answers).pool()))
    script = [
        transfer("energy_analyst", "Cost and consumption for Baruch Houses, Q1 2024 against Q1 2023"),
        Turn.call("get_utility_bills_with_historical_data", {}),
        transfer("report_writer", "Costs up 12 percent; no data gaps"),
        Turn.say("# Report\nCosts rose 12 percent.\nSEVERITY: 3"),
    ]
    export = DEV.settings.observability.export.model_copy(update={"retry_backoff_milliseconds": 5})
    app = make_app(
        script, workflows={TEAM: facility_team()}, ports=ports, tracing=True, observability={"export": export}
    )
    assert app.deps.tracing is not None and app.obs is not None
    app.deps.tracing.start()
    try:
        async with app.client() as client:
            body = {"agenticWorkflowId": TEAM, "input": {"text": "Why did costs rise?"}, "mode": "stream"}
            response = await client.post("/v1/runs", json=body, headers=auth())
        await app.deps.tracing.flush()
    finally:
        await app.deps.tracing.aclose()
    events = sse_events(response.text)
    assert events[-1]["data"]["result"] == {
        "report": "# Report\nCosts rose 12 percent.\nSEVERITY: 3",
        "finalAgent": "report_writer",
        "history": ["coordinator", "energy_analyst", "report_writer"],
    }
    [bills] = [e for e in of(events, "tool.started") if e["data"]["toolName"].startswith("get_utility")]
    assert bills["nodeId"] == "swarm_ops" and bills["data"]["member"] == "energy_analyst"
    # The LLM member is an agent with no tools of its own: only its transfers.
    assert sorted(app.models.model.requests[-1].tool_names) == [
        "transfer_to_coordinator",
        "transfer_to_energy_analyst",
        "transfer_to_facility_analyst",
        "transfer_to_risk_analyst",
    ]
    found = list(app.obs.observations.values())
    steps = [o["nodeId"] for o in found if o.get("visit") and o.get("nodeId") in FACILITY_MEMBERS]
    assert sorted(steps) == ["agent_coordinator", "agent_energy", "llm_writer"]
    assert len([o for o in found if o.get("name") == "swarm.handoff"]) == 2


async def test_on_handoff_hooks_see_each_handoff_on_the_swarm_node() -> None:
    from tests.unit.test_hooks import CALLS, calls

    CALLS.clear()
    bindings = [
        binding("seen", "onHandoff", match={"nodeNames": ["support_team"]}, config={"label": "handoff"})
    ]
    h, _ = agent_harness([transfer("billing"), transfer("tech_support"), Turn.say("Restart it.")], DEV)
    h.hooks = template_hooks(bindings)
    await h.run(support()["document"])
    assert calls("onHandoff") == [("swarm_1", "handoff"), ("swarm_1", "handoff")]
