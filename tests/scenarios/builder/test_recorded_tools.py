"""Case F-09 (P3-12): builder workflows' tools answered from the recordings (vendored from the
Node.js runtime, decision 0006). Each scenario's flow is a recorded context of ``contexts.json``,
so every tool call finds its recording; an unrecorded call fails the run.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from tests.api.app import auth, make_app, sse_events
from tests.fakes.egress import FakeEgress, http_parameters
from tests.fakes.mcp_server import FakeMcpServer, ok
from tests.fakes.recordings import RECORDINGS_DIR, Recordings, parameter_hash, template_aliases
from tests.fakes.scripted_chat_model import Request, Turn
from tests.scenarios.builder.test_builder_scenarios import RESOURCES
from tests.workflow_fixtures import BUILDER, read_json
from zonic_runtime.engine.node_template import ServicePorts
from zonic_runtime.integrations.mcp.execute import McpService
from zonic_runtime.ports.egress import EgressRequest

RECORDED = RECORDINGS_DIR / "documents"
#: The red hook context of contexts.json: Q1 2024 against Q1 2023.
KEYWORDS = json.dumps(
    {
        "newPeriodStart": "2024-01-01",
        "newPeriodEnd": "2024-03-31",
        "facilityName": "red hook",
        "historicalReferencePeriodStart": "2023-01-01",
        "historicalReferencePeriodEnd": "2023-03-31",
    }
)
ENERGY_PATH = [
    "Start_1",
    "keywords-extraction",
    "energy-agent",
    "get-historical-data-with-reference",
    "Rule_1",
    "no-analysis",
    "utility-analysis",
    "utility-bills-variable-update",
    "Output_1",
]


def energy_model() -> list[Any]:
    """The Node.js ``energyModel``: answers by which tools each model call is offered."""
    keywords = False

    def answer(request: Request) -> Turn:
        nonlocal keywords
        said = json.dumps([m.model_dump() for m in request.messages], default=str)
        if "fn_get_bill_summary" in request.tool_names:  # the energy agent
            if "bills-1" not in said:
                return Turn.call("get_utility_bills_with_historical_data", {}, id="bills-1")
            if "handoff-1" not in said:
                return Turn.call(
                    "handoff_to", {"target": "get-historical-data-with-reference"}, id="handoff-1"
                )
            return Turn.say("Handing over to the analysis.")
        # The Zonic copy offers its function tool; the open-data copy an API stand-in.
        analysis = next(
            (
                n
                for n in ("analyse_energy_performance", "analyse_energy_performance_api")
                if n in request.tool_names
            ),
            None,
        )
        if analysis is not None:
            if "perf-1" not in said:
                return Turn.call(analysis, {}, id="perf-1")
            return Turn.say(
                json.dumps({"analysisType": "other", "analysisLevel": "site", "aggregationOption": "monthly"})
            )
        if not keywords:
            keywords = True
            return Turn.say(KEYWORDS)
        return Turn.say("No analysis is needed for this period.")

    return [answer] * 20


async def run_energy(document: dict[str, Any], mcp: FakeMcpServer) -> list[dict[str, Any]]:
    ports = ServicePorts(mcp=McpService(mcp.pool()))
    workflow = "energy-recorded"
    app = make_app(energy_model(), resources=RESOURCES, workflows={workflow: document}, ports=ports)
    async with app.client() as client:
        body = {
            "agenticWorkflowId": workflow,
            "input": {"text": "Electricity at red hook?"},
            "mode": "stream",
        }
        response = await client.post("/v1/runs", json=body, headers=auth())
        assert response.status_code == 200, response.text
        return sse_events(response.text)


@pytest.mark.parametrize("copy", ["open-data", "zonic"])
async def test_F09_energy_tools_take_their_values_from_flow_and_are_answered_by_the_recordings(
    copy: str,
) -> None:
    recorded = read_json(RECORDED / "open-data" / "fsh-energy-agent.json")
    document = recorded if copy == "open-data" else read_json(BUILDER / copy / "fsh-energy-agent.json")
    mcp = FakeMcpServer(
        # The Zonic copy's Django-only function tool (an API stand-in in the recorded copy).
        answers={"analyse_energy_performance": ok(None, {"chart_json": {"series": []}})},
        recordings=Recordings.of("fsh-energy-agent"),
        # The Zonic copy's tools run the same queries in T-SQL: they borrow the PostgreSQL answers.
        template_aliases=template_aliases(document, recorded),
    )
    events = await run_energy(document, mcp)
    assert events[-1]["type"] == "run.completed", events[-1]
    names = {n["id"]: n.get("name") or n["id"] for n in (document.get("document") or document)["nodes"]}
    assert [names[e["nodeId"]] for e in events if e["type"] == "node.started"] == ENERGY_PATH
    assert mcp.unrecorded == []
    [bills] = [c for c in mcp.tool_calls if c["tool"] == "get_utility_bills_with_historical_data"]
    # userID is the workflow's own variable; the period and facility come from the keywords.
    assert {
        k: bills["parameters"][k]
        for k in (
            "userID",
            "newPeriodStart",
            "newPeriodEnd",
            "facilityName",
            "historicalReferencePeriodStart",
        )
    } == {
        "userID": "1809",
        "newPeriodStart": "2024-01-01",
        "newPeriodEnd": "2024-03-31",
        "facilityName": "red hook",
        "historicalReferencePeriodStart": "2023-01-01",
    }
    assert ("DECLARE @" if copy == "zonic" else "sample_open_data") in str(bills["config"]["script"])
    # The agent's copy of the bills tool filters by user and period, not by facility, so the
    # bills kept for the flow are every facility's: 1,787 rows.
    assert sorted(len(v) for v in mcp.cache.values() if isinstance(v, list))[-1] == 1787
    analysis = next(c for c in mcp.tool_calls if c["tool"].startswith("analyse_energy_performance"))
    assert analysis["tool"] == (
        "analyse_energy_performance_api" if copy == "open-data" else "analyse_energy_performance"
    )


def test_F09_every_recording_is_found_by_the_key_this_runtime_computes() -> None:
    """The file names (tool and parameter hash) are the Node.js ones, so both replay one set."""
    for folder in sorted(p for p in RECORDINGS_DIR.iterdir() if p.is_dir() and p.name != "documents"):
        recordings = Recordings.of(folder.name)
        assert len(recordings) == len(list(folder.glob("*.json")))
        for recording in recordings.all():
            template = recording.get("template") or ""
            assert parameter_hash(recording["parameters"], template) in {
                p.name.split(".")[-2] for p in folder.glob(f"{recording['tool']}.*.json")
            }
            assert recordings.find(recording["tool"], recording["parameters"], template) is recording


async def test_F09_fake_egress_replays_the_http_recordings_and_fails_an_unrecorded_call() -> None:
    workflows = ("fsh-facility-agent", "fsh-risk-agent", "facility_operations_swarm")
    egress = FakeEgress.from_recordings(RESOURCES.egress, *workflows)
    answered = 0
    for recording in Recordings.of(*workflows).all():
        if recording["tool"] != "http":
            continue
        request = EgressRequest(method=recording["parameters"]["method"], url=recording["parameters"]["url"])
        assert http_parameters(request) == recording["parameters"]
        response = await egress.send(request)
        assert response.status == recording["response"]["http"]["status"] == 200
        assert json.loads(response.text()) is not None
        answered += 1
    assert answered == 3
    with pytest.raises(AssertionError, match="unrecorded HTTP call"):
        await egress.send(EgressRequest(method="GET", url="https://httpbin.org/never-recorded"))
    assert not egress.allows("https://elsewhere.test/")
