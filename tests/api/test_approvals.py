"""Tool approvals and the ``approval`` hook (P4-03: H-06, K-11; W-11 through the app).

An agent with the built-in ``calculator`` that ``settings.tools.requireApproval`` names, so each call
pauses the run for a decision; ``approval`` bindings before a node and before a tool call; and the
Customer Support Swarm with ``current_time`` gated, for a member that pauses mid-step.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from langchain_core.messages import ToolMessage

from tests.api.app import DEV, auth, make_app, sse_events
from tests.api.test_swarm import support, transfer
from tests.fakes.scripted_chat_model import Turn
from zonic_runtime.models.hooks.binding import HookBindings

FLOW = "wf-tools"
GATED = {"require_approval": ["calculator"]}
AGENT = {
    "schemaVersion": "2.0",
    "nodes": [
        {"id": "start", "type": "start", "name": "Start", "config": {}},
        {
            "id": "ag",
            "type": "agent",
            "name": "ag",
            "config": {
                "systemPrompt": "Do the maths.",
                "tools": [{"name": "calculator", "type": "strands_builtin", "config": {}}],
            },
        },
        {
            "id": "out",
            "type": "output",
            "name": "out",
            "config": {"outputMapping": [{"target": "answer", "source": "{{nodes.ag.output.text}}"}]},
        },
    ],
    "edges": [{"source": "start", "target": "ag"}, {"source": "ag", "target": "out"}],
}
SCRIPT = [Turn.call("calculator", {"expression": "2+2"}), Turn.say("Done.")]


async def decide(
    decision: dict[str, Any], **settings: Any
) -> tuple[Any, list[dict[str, Any]], dict[str, Any]]:
    """Runs the agent until its calculator call waits, answers it, and returns the app, the
    pending interrupt as listed, and the finished turn."""
    app = make_app(
        SCRIPT, workflows={FLOW: AGENT}, tools={**DEV.settings.tools.model_dump(), **GATED}, **settings
    )
    async with app.client() as client:
        body = {"agenticWorkflowId": FLOW, "input": {"text": "what is 2+2?"}, "mode": "sync"}
        waiting = (await client.post("/v1/runs", json=body, headers=auth())).json()
        assert waiting["status"] == "waiting_input", waiting
        run_id = waiting["runId"]
        listed = (await client.get(f"/v1/runs/{run_id}/interrupts", headers=auth())).json()["items"]
        [pending] = listed
        answer = {"answer": decision, "mode": "sync"}
        done = await client.post(
            f"/v1/runs/{run_id}/interrupts/{pending['interruptId']}", json=answer, headers=auth()
        )
    return app, listed, done.json()


def tool_result(app: Any) -> ToolMessage:
    [message] = [m for m in app.models.model.requests[-1].messages if isinstance(m, ToolMessage)]
    return message


# --- H-06 ---------------------------------------------------------------------------------------


async def test_H06_an_approved_tool_call_runs() -> None:
    app, [pending], done = await decide({"type": "approve"})
    assert pending["kind"] == "tool_approval" and pending["nodeId"] == "ag"
    assert pending["prompt"]["tool"] == "calculator" and pending["prompt"]["args"] == {"expression": "2+2"}
    assert done["status"] == "succeeded" and tool_result(app).text == '{"result": 4}'
    assert len(app.models.model.requests) == 2  # the model's first turn was not repeated


async def test_H06_an_edited_tool_call_runs_with_the_reviewers_arguments() -> None:
    edit = {"type": "edit", "edited_action": {"name": "calculator", "args": {"expression": "3*3"}}}
    app, _, done = await decide(edit)
    assert done["status"] == "succeeded" and tool_result(app).text.endswith('{"result": 9}')


async def test_H06_a_rejected_tool_call_never_runs_and_the_model_is_told() -> None:
    app, _, done = await decide({"type": "reject", "message": "No maths today."})
    result = tool_result(app)
    assert done["status"] == "succeeded" and result.status == "error"
    assert "No maths today." in result.text and "result" not in result.text


async def test_H06_a_decision_outside_the_schema_is_refused() -> None:
    app = make_app(SCRIPT, workflows={FLOW: AGENT}, tools={**DEV.settings.tools.model_dump(), **GATED})
    async with app.client() as client:
        body = {"agenticWorkflowId": FLOW, "input": {"text": "what is 2+2?"}, "mode": "sync"}
        run_id = (await client.post("/v1/runs", json=body, headers=auth())).json()["runId"]
        [pending] = (await client.get(f"/v1/runs/{run_id}/interrupts", headers=auth())).json()["items"]
        wrong = {"answer": {"type": "edit", "edited_action": {"name": "other", "args": {}}}, "mode": "sync"}
        refused = await client.post(
            f"/v1/runs/{run_id}/interrupts/{pending['interruptId']}", json=wrong, headers=auth()
        )
    assert refused.status_code == 400 and refused.json()["error"]["code"] == "INTERRUPT_ANSWER_INVALID"


# --- K-11 ---------------------------------------------------------------------------------------


def hooks(*bindings: dict[str, Any]) -> Any:
    return replace(DEV, hooks=HookBindings.model_validate({"bindings": list(bindings)}))


async def run_with(
    resources: Any, decision: dict[str, Any], script: list[Any]
) -> tuple[Any, dict[str, Any], dict[str, Any]]:
    app = make_app(script, resources=resources, workflows={FLOW: AGENT})
    async with app.client() as client:
        body = {"agenticWorkflowId": FLOW, "input": {"text": "what is 2+2?"}, "mode": "stream"}
        response = await client.post("/v1/runs", json=body, headers=auth())
        events = sse_events(response.text)
        assert events[-1]["type"] == "run.waiting", events[-1]
        run_id = response.headers["x-run-id"]
        [pending] = (await client.get(f"/v1/runs/{run_id}/interrupts", headers=auth())).json()["items"]
        answer = {"answer": decision, "mode": "sync"}
        done = await client.post(
            f"/v1/runs/{run_id}/interrupts/{pending['interruptId']}", json=answer, headers=auth()
        )
    return app, pending, done.json()


BEFORE_NODE = {
    "id": "ask-first",
    "hook": "approval",
    "on": ["beforeNode"],
    "match": {"nodeNames": ["ag"]},
    "config": {
        "prompt": "Run the agent for {{system.userQuery}}?",
        "rejectedOutput": {"text": "Not approved."},
    },
}
BEFORE_TOOL = {
    "id": "ask-tool",
    "hook": "approval",
    "on": ["beforeToolCall"],
    "match": {"nodeNames": ["ag"]},
    "config": {"prompt": "Let the agent calculate?", "message": "The reviewer said no."},
}


async def test_K11_an_approval_hook_before_a_node_waits_then_runs_it() -> None:
    _, pending, done = await run_with(hooks(BEFORE_NODE), {"approved": True}, SCRIPT)
    assert pending["kind"] == "approval" and pending["prompt"]["text"] == "Run the agent for what is 2+2??"
    assert done["status"] == "succeeded" and done["result"] == {"answer": "Done."}


async def test_K11_a_rejected_node_approval_follows_the_configured_output_and_handle() -> None:
    app, _, done = await run_with(hooks(BEFORE_NODE), {"approved": False}, SCRIPT)
    assert done["status"] == "succeeded" and done["result"] == {"answer": "Not approved."}
    assert app.models.model.requests == []  # the agent never ran


async def test_K11_an_approval_hook_before_a_tool_rejects_the_call() -> None:
    app, pending, done = await run_with(hooks(BEFORE_TOOL), {"approved": False}, SCRIPT)
    assert pending["prompt"]["tool"] == "calculator" and pending["prompt"]["args"] == {"expression": "2+2"}
    result = tool_result(app)
    assert (
        done["status"] == "succeeded" and result.text == "The reviewer said no." and result.status == "error"
    )


async def test_K11_an_approved_tool_call_hook_lets_the_tool_run() -> None:
    app, _, done = await run_with(hooks(BEFORE_TOOL), {"approved": True}, SCRIPT)
    assert done["status"] == "succeeded" and tool_result(app).text == '{"result": 4}'


# --- W-11 ---------------------------------------------------------------------------------------


async def test_W11_a_member_tool_approval_pauses_the_swarm_and_the_same_member_continues() -> None:
    tools = {**DEV.settings.tools.model_dump(), "require_approval": ["current_time"]}
    script = [transfer("tech_support"), Turn.call("current_time", {}), Turn.say("It is noon.")]
    app = make_app(script, workflows={"wf-support": support()}, tools=tools)
    async with app.client() as client:
        body = {"agenticWorkflowId": "wf-support", "input": {"text": "What time is it?"}, "mode": "stream"}
        response = await client.post("/v1/runs", json=body, headers=auth())
        events = sse_events(response.text)
        assert events[-1]["type"] == "run.waiting"
        run_id, [pending] = response.headers["x-run-id"], events[-1]["data"]["interruptIds"]
        assert len(app.models.model.requests) == 2  # triage, then tech_support's call
        answer = {"answer": {"type": "approve"}, "mode": "stream"}
        resumed = sse_events(
            (await client.post(f"/v1/runs/{run_id}/interrupts/{pending}", json=answer, headers=auth())).text
        )
    assert len(app.models.model.requests) == 3  # only tech_support's turn after the tool's result
    assert resumed[-1]["data"]["result"] == {
        "answer": "It is noon.",
        "finalAgent": "tech_support",
        "history": ["triage", "tech_support"],
    }
    assert [e["data"]["member"] for e in resumed if e["type"] == "swarm.member.started"] == ["tech_support"]
