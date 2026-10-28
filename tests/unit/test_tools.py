"""The tool factory (P3-02: N-05 tools part, T-03 to T-05, T-07, T-09) with the real ``McpPool``
over the in-process FastMCP server, the real agent kind and the scripted model."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from typing import Any

import pytest

from tests.fakes.mcp_server import FakeMcpServer, failed, ok
from tests.fakes.scripted_chat_model import ScriptedChatModel, Turn
from tests.kinds.agents import ScriptedModels
from tests.kinds.harness import DEFAULT_INPUT, Harness
from zonic_runtime.config.env import EnvironmentModel
from zonic_runtime.config.settings import load_resources
from zonic_runtime.engine.agents.agent_factory import AgentServices
from zonic_runtime.engine.agents.tool_factory import calculate
from zonic_runtime.engine.context import RunPorts, ToolCallKeys
from zonic_runtime.engine.kinds.core import core_kinds
from zonic_runtime.errors import ZonicError
from zonic_runtime.integrations.mcp.execute import McpService
from zonic_runtime.ports.mcp import RunMcp
from zonic_runtime.workflow.loader.loader import WorkflowLoader

DEV = load_resources(EnvironmentModel(env="dev"))


def database_tool(**settings: Any) -> dict[str, Any]:
    """The builder's MCP block with one configured database tool."""
    tool = {
        "id": "t1",
        "name": "bills_query",
        "toolType": "database",
        "description": "Utility bills of a site.",
        "enabled": True,
        "script": "select * from bills where site = :site",
        "connectionId": "pg_open_data",
        "parameters": [
            {
                "name": "site",
                "dataType": "string",
                "source": "llm",
                "required": True,
                "description": "The site.",
            },
            {"name": "period", "source": "flow", "value": "{{flow.period}}"},
        ],
        "result": {"saveToCache": True, "saveToFlowKey": "flow.billsKey"},
        **settings,
    }
    return {
        "type": "mcp",
        "name": "db",
        "description": "",
        "mcp": {"serverName": "default", "url": "default", "configuredTools": [tool]},
    }


def document(*tools: dict[str, Any]) -> dict[str, Any]:
    agent = {
        "id": "ag",
        "type": "agent",
        "name": "ag",
        "config": {"systemPrompt": "You help.", "tools": list(tools)},
    }
    return {
        "schemaVersion": "2.0",
        "nodes": [{"id": "start", "type": "start", "config": {}}, agent],
        "edges": [{"source": "start", "target": "ag"}],
    }


def harness(
    script: list[Any], fake: FakeMcpServer | None = None, secrets: Any = None, **tools: Any
) -> tuple[Harness, ScriptedModels]:
    models = ScriptedModels(ScriptedChatModel(script=script))
    resources = DEV
    if tools:
        tool_settings = DEV.settings.tools.model_copy(update=tools)
        resources = replace(DEV, settings=DEV.settings.model_copy(update={"tools": tool_settings}))
    agents = AgentServices.from_resources(resources, builders=models.builders())  # the real ToolFactory
    service = McpService(fake.pool()) if fake is not None else None

    def ports(run_id: str) -> RunPorts:
        return RunPorts(mcp=RunMcp(service, run_id, "owner") if service else None, secrets=secrets)

    return Harness(resources, core_kinds(), agents=agents, run_ports=ports), models


def flow(**values: Any) -> dict[str, Any]:
    return {**DEFAULT_INPUT, "flow": {"values": values, "provenance": {}, "conflicts": []}}


def tool_messages(models: ScriptedModels) -> list[Any]:
    return [m for m in models.model.requests[-1].messages if m.type == "tool"]


# --- N-05 (tools part) ---------------------------------------------------------------------------


async def test_N05_a_database_tool_runs_through_mcp_execute_and_its_flow_update_is_applied() -> None:
    fake = FakeMcpServer(answers={"bills_query": ok("flow.billsKey", "cache:bills:v1:abc", cached=True)})
    h, models = harness([Turn.call("bills_query", {"site": "Main"}), Turn.say("Saved the bills.")], fake)
    final, *_ = await h.run(document(database_tool()), flow(period="2026-09"))
    [call] = fake.calls
    assert call["requestId"] == "run-1:ag:1:0"  # runId:nodeId:visit:callIndex (DR-2)
    assert (
        call["context"]["flow"] == {"period": "2026-09"} and call["context"]["system"]["ownerSub"] == "owner"
    )
    [tool] = call["tools"]
    assert (
        tool["type"] == "database"
        and tool["input"] == {"site": "Main"}
        and tool["config"]["name"] == "bills_query"
    )
    assert final["flow"]["values"]["billsKey"] == "cache:bills:v1:abc"  # the flow update reached the run
    [reply] = tool_messages(models)
    assert json.loads(reply.content) == {
        "saved": True,
        "variable": "flow.billsKey",
        "key": "cache:bills:v1:abc",
    }
    out = final["nodes"]["ag"]["output"]
    assert out["text"] == "Saved the bills." and [c["name"] for c in out["toolCalls"]] == ["bills_query"]


async def test_N05_a_failing_tool_is_an_error_the_model_sees_and_the_node_goes_on() -> None:
    fake = FakeMcpServer(answers={"bills_query": failed("connection refused by the database")})
    h, models = harness(
        [Turn.call("bills_query", {"site": "Main"}), Turn.say("The data is unavailable.")], fake
    )
    final, *_ = await h.run(document(database_tool()))
    [reply] = tool_messages(models)
    assert reply.status == "error" and "connection refused" in reply.content
    assert final["nodes"]["ag"]["status"] == "succeeded" and "billsKey" not in (final.get("flow") or {}).get(
        "values", {}
    )


async def test_N05_without_an_mcp_server_the_tool_errors_but_the_node_does_not_fail() -> None:
    h, models = harness([Turn.call("bills_query", {"site": "Main"}), Turn.say("Could not look it up.")])
    final, *_ = await h.run(document(database_tool()))
    assert (
        "not configured" in tool_messages(models)[0].content and final["nodes"]["ag"]["status"] == "succeeded"
    )


# --- T-03 --------------------------------------------------------------------------------------


async def test_T03_an_input_that_violates_the_tool_schema_is_an_error_result_the_model_can_correct() -> None:
    fake = FakeMcpServer(answers={"bills_query": ok("flow.billsKey", "k")})
    script = [
        Turn.call("bills_query", {"site": 42}),
        Turn.call("bills_query", {"site": "Main"}),
        Turn.say("Done."),
    ]
    h, models = harness(script, fake)
    await h.run(document(database_tool()))
    first, second = tool_messages(models)
    assert (
        first.status == "error"
        and "invalid input for bills_query" in first.content
        and "/site" in first.content
    )
    assert second.status == "success" and len(fake.calls) == 1  # only the corrected call reached the server


# --- T-04 --------------------------------------------------------------------------------------


def test_T04_a_literal_secret_in_tool_settings_is_document_invalid() -> None:
    loader = WorkflowLoader.from_resources(DEV)
    leaked = document(database_tool(headers={"Authorization": "Bearer abc123"}, apiKey="plain-key"))
    with pytest.raises(ZonicError) as caught:
        loader.from_stored(leaked, "wf")
    assert caught.value.code == "DOCUMENT_INVALID" and "literal secret" in json.dumps(caught.value.details)
    loader.from_stored(
        document(database_tool(apiKey="secret:db_key", headers={"Authorization": "{{secrets.api}}"})), "wf"
    )


async def test_T04_a_secret_reference_is_resolved_for_the_call_and_never_reaches_events_or_state() -> None:
    class Secrets:
        def get(self, name: str) -> str | None:
            return {"db_key": "s3cr3t-value"}.get(name)

    fake = FakeMcpServer(answers={"bills_query": ok("flow.billsKey", "k")})
    h, _ = harness([Turn.call("bills_query", {"site": "Main"}), Turn.say("ok")], fake, secrets=Secrets())
    final, events, *_ = await h.run(document(database_tool(apiKey="secret:db_key")))
    assert fake.calls[0]["tools"][0]["config"]["apiKey"] == "s3cr3t-value"
    assert "s3cr3t-value" not in json.dumps(events, default=str) and "s3cr3t-value" not in json.dumps(
        final, default=str
    )


async def test_T04_a_missing_secret_is_a_tool_error_naming_the_reference_only() -> None:
    fake = FakeMcpServer(answers={"bills_query": ok("flow.billsKey", "k")})
    h, models = harness([Turn.call("bills_query", {"site": "Main"}), Turn.say("ok")], fake, secrets=None)
    await h.run(document(database_tool(apiKey="secret:db_key")))
    reply = tool_messages(models)[0]
    assert reply.status == "error" and "TOOL_FAILED" in reply.content and fake.calls == []


# --- T-05 --------------------------------------------------------------------------------------


async def test_T05_a_built_in_tool_off_the_allow_list_is_tool_not_allowed() -> None:
    shell = {"type": "strands_builtin", "name": "shell", "config": {}}
    h, _ = harness([Turn.say("never")])
    with pytest.raises(ZonicError) as caught:
        await h.run(document(shell))
    assert caught.value.code == "TOOL_NOT_ALLOWED" and caught.value.details["tool"] == "shell"


async def test_T05_an_allow_listed_built_in_runs_in_the_runtime() -> None:
    calculator = {"type": "strands_builtin", "name": "calculator", "config": {}}
    h, models = harness([Turn.call("calculator", {"expression": "(3 + 4) * 2 ^ 2"}), Turn.say("28")])
    await h.run(document(calculator))
    assert json.loads(tool_messages(models)[0].content) == {"result": 28}


@pytest.mark.parametrize(
    ("expression", "value"), [("1 + 2 * 3", 7), ("-(2 - 5) / 2", 1.5), ("10 % 4", 2), ("2 ^ 3 ^ 2", 512)]
)
def test_T05_the_calculator_evaluates_without_eval(expression: str, value: float) -> None:
    assert calculate(expression) == value


@pytest.mark.parametrize("expression", ["__import__('os')", "1 / 0", "2 +", "1e999 * 10"])
def test_T05_the_calculator_refuses_anything_but_arithmetic(expression: str) -> None:
    from langchain_core.tools import ToolException

    with pytest.raises(ToolException):
        calculate(expression)


# --- T-07 --------------------------------------------------------------------------------------


async def test_T07_concurrent_tool_calls_are_bounded_by_permits_and_paired_by_call_id() -> None:
    fake = FakeMcpServer(answers={"bills_query": ok("flow.billsKey", "k")}, delay=0.05)
    script = [Turn.calls(*[("bills_query", {"site": f"S{i}"}) for i in range(6)]), Turn.say("All six.")]
    h, models = harness(script, fake, max_concurrent_per_turn=2)
    await h.run(document(database_tool()))
    assert fake.peak == 2 and len(fake.calls) == 6
    calls = [c["id"] for c in models.model.requests[-1].messages[-7].tool_calls]  # the model's six calls
    assert sorted(m.tool_call_id for m in tool_messages(models)) == sorted(calls)


async def test_T07_cancelling_the_run_reaches_the_tool_call_on_the_server() -> None:
    fake = FakeMcpServer(answers={"bills_query": ok("flow.billsKey", "k")}, delay=5)
    h, _ = harness([Turn.call("bills_query", {"site": "Main"}), Turn.say("never")], fake)
    task = asyncio.create_task(h.run(document(database_tool())))
    for _ in range(100):
        if fake.in_flight:
            break
        await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    for _ in range(100):
        if fake.cancelled:
            break
        await asyncio.sleep(0.01)
    assert fake.cancelled == 1


# --- T-09 --------------------------------------------------------------------------------------


async def test_T09_keys_count_a_visits_calls_in_issue_order_and_repeat_when_the_visit_is_re_executed() -> (
    None
):
    fake = FakeMcpServer(answers={"bills_query": ok("flow.billsKey", "k")})

    def script() -> list[Any]:
        return [Turn.calls(("bills_query", {"site": "A"}), ("bills_query", {"site": "B"})), Turn.say("ok")]

    for _ in range(2):  # the same visit re-executed: the model's call IDs differ, the keys do not
        h, _models = harness(script(), fake)
        await h.run(document(database_tool()), run_id="run-7")
    keys = [c["requestId"] for c in fake.calls]
    assert keys[:2] == ["run-7:ag:1:0", "run-7:ag:1:1"] and keys[2:] == keys[:2]


def test_T09_an_attempt_counts_its_calls_from_zero_again() -> None:
    keys = ToolCallKeys()
    keys.start("ag", 1)
    assert [keys.assign("ag", 1, c) for c in ("a", "b")] == ["ag:1:0", "ag:1:1"]
    keys.start("ag", 1)  # a retry of the same visit
    assert [keys.assign("ag", 1, c) for c in ("c", "d")] == ["ag:1:0", "ag:1:1"] and keys.key("a") == "ag:1:0"
