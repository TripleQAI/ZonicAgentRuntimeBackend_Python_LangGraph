"""The builder scenarios (plan P2-09, Section 20.4): one per vendored workflow whose node kinds exist.

A scenario is a vendored document, the user's text and the scripted model turns of each turn, and
the path of node names the turn takes. ``WAITING`` names, for every node kind not built yet, the
task that builds it; a vendored workflow that needs one of those kinds waits for that task, and
the coverage test fails as soon as the task is complete and the scenario still missing.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from tests.fakes.mcp_server import Answer, ok
from tests.fakes.scripted_chat_model import Turn
from tests.scenarios.builder import platform

#: Node kind -> the task that builds it (plan Section 15). The scenarios of workflows using these
#: kinds join with their task.
WAITING = MappingProxyType(
    {
        "mcp_execute": "P3-04",
        "tool": "P3-06",
        "guardrail": "P3-06",
        "external_agent": "P3-06",
        "multi_agent": "P4-01",
        "interrupt": "P4-02",
        #: Not a kind: configured agent and LLM tools (MCP, builder rows, built-ins).
        "tools": "P3-02",
    }
)

#: Vendored workflows with a sub-flow child that was never vendored: the gate accepts their
#: ``missing-child`` until the child is added or the scenario avoids that branch.
FIXTURE_GAPS = MappingProxyType(
    {
        "platform/intelligent-buying-orchestrator": (
            "sub-flow exec_category_agnostic (025421e9-551a-4034-aea5-7ebce5fc9d50) is not vendored"
        ),
    }
)

Script = Sequence[Turn | Callable[[Any], Turn]]


@dataclass(frozen=True)
class ScenarioTurn:
    text: str
    script: Script
    #: Node names in the order their visits started.
    path: tuple[str, ...]
    #: Answers to the turn's interrupts, in order: each resumes the run waiting for it (P4-02).
    answers: tuple[Any, ...] = ()


@dataclass(frozen=True)
class Scenario:
    name: str
    #: Under ``tests/fixtures/workflows/builder``.
    fixture: str
    turns: tuple[ScenarioTurn, ...] = ()
    #: A start that must be refused, with this catalog code.
    refused: str | None = None
    #: Nodes the blueprint reports unreachable and never compiles (F-07).
    unreachable: tuple[str, ...] = ()
    #: ``WAITING`` entries this scenario knowingly runs without (a path that avoids them).
    waits: tuple[str, ...] = ()
    #: Fixtures published beside it: its sub-flows' children, under their own IDs.
    children: tuple[str, ...] = ()
    #: The MCP ``execute`` answers by configured tool name; F-09 replays the recordings instead
    #: (``test_recorded_tools.py``, P3-12).
    mcp: Mapping[str, Answer] = field(default_factory=dict)
    #: Workflows whose recordings answer the MCP tools and HTTP calls not in ``mcp`` (P3-12).
    recordings: tuple[str, ...] = ()
    #: A fresh model responder per run, for every turn, instead of the turns' scripts: platform
    #: agents run in an order their rules decide, so the model answers by what each call offers.
    responder: Callable[[], Callable[[Any], Turn]] | None = None
    #: The answers of MCP servers' own tools by MCP tool name (platform ``tool`` nodes and tools);
    #: with it, ``execute`` tools without an answer get ``{"ok": true}``.
    server_tools: Mapping[str, Any] | None = None
    #: Script nodes run their code with the local interpreter (``FakePiston``).
    sandbox: bool = False
    notes: str = ""


QUESTION = ScenarioTurn(
    text="What is six times seven?",
    script=[Turn.say("Six times seven is 42.")],
    path=("Start_1", "Agent_1", "Output_1"),
)
CALCULATED = ScenarioTurn(
    text="And 17 times 23?",
    script=[Turn.call("calculator", {"expression": "17 * 23"}), Turn.say("17 times 23 is 391.")],
    path=("Start_1", "Agent_1", "Output_1"),
)

#: The energy analysis the ``analyse_energy_performance`` function returns (chart and summary).
ENERGY = {
    "summary": {"site": "Main", "change_pct": 12.4},
    "chart_json": {
        "title": {"text": "Electricity use"},
        "xAxis": {"type": "category", "data": ["Jan", "Feb", "Mar"]},
        "yAxis": {"type": "value"},
        "series": [{"type": "bar", "data": [410, 455, 470]}],
    },
}
ENERGY_TURN = ScenarioTurn(
    text="Was electricity use at Main higher than last year in Q1?",
    script=[
        Turn.say(
            '{"newPeriodStart": "2026-01-01", "newPeriodEnd": "2026-03-31",'
            ' "historicalReferencePeriodStart": "2025-01-01", "historicalReferencePeriodEnd": "2025-03-31"}'
        ),
        Turn.call(
            "handoff_to",
            {"target": "get-historical-data-with-reference", "reason": "variance against last year"},
            text='{"analysisType": "usage_variance_higher_lower", "analysisLevel": "site", "aggregationOption": "monthly"}',
        ),
        Turn.call("analyse_energy_performance", {"site": "Main", "metric": "electricity"}),
        Turn.say("The analysis is ready."),
        Turn.say('{"dataAnalysisResult": "<p>Use was <b>12.4%</b> higher than in Q1 2025.</p>"}'),
    ],
    path=(
        "Start_1",
        "keywords-extraction",
        "energy-agent",
        "get-historical-data-with-reference",
        "Rule_1",
        "usage-consumption-analysis",
        "utility-analysis",
        "utility-bills-variable-update",
        "Output_1",
    ),
)
ENERGY_MCP: Mapping[str, Answer] = MappingProxyType(
    {"analyse_energy_performance": ok("flow.energyPerformanceAnalysisResult", ENERGY)}
)


def analysis_turn(agent: str) -> ScenarioTurn:
    """fsh-facility-agent and fsh-risk-agent: the agent hands off to the analysis LLM and its view."""
    return ScenarioTurn(
        text="Which facilities are over budget?",
        script=[
            Turn.call(
                "handoff_to", {"target": "data-analysis", "reason": "needs analysis"}, text="Analysing."
            ),
            Turn.say("Three facilities exceed their budget."),
        ],
        path=("Start_1", agent, "data-analysis", "report", "Output_1"),
    )


def sql_turn(agent: str, branch: str) -> ScenarioTurn:
    """The SQL branch: the generator writes a query, then ``Rule_1`` routes on ``flow.useDBQuery``
    (fsh-risk-agent declares it ``"true"``: the DB Query; fsh-facility-agent leaves it unset: HTTP)."""
    return ScenarioTurn(
        text="Which facilities cost the most?",
        script=[
            Turn.call("handoff_to", {"target": "sql-query-generator", "reason": "needs a query"}),
            Turn.say("SELECT * FROM sample_open_data.facility LIMIT 10"),
        ],
        path=("Start_1", agent, "sql-query-generator", "Rule_1", branch, "Output_1"),
    )


#: The rows a Zonic copy's DB Query node gets (the SQL Server queries have no recordings).
FACILITY_ROWS: Mapping[str, Answer] = MappingProxyType(
    {"query-analysis": ok(None, [{"facility_name": "Red Hook", "borough": "BROOKLYN"}])}
)

#: The platform's state-machine answer: the policy agent's ``get_next_questions`` sets
#: ``current_state`` (F-17).
PLATFORM_TOOLS: Mapping[str, Any] = MappingProxyType(
    {"Get_Next_Questions": {"result": {"current_state": "quickPaySetup", "user_decision_points": "amount"}}}
)
QUICK_PAY = (
    "quick_pay",
    "quick_pay/start",
    "quick_pay/rule_0",
    "quick_pay/knowledge_agent",
    "quick_pay/quick_pay",
    "quick_pay/output",
)
#: Each platform sub-agent's path through its loop twice (F-18, B-13): the nodes before its agent,
#: then the agent, the loop, the agent, the loop, the agent, the Output node.
SUB_AGENTS: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        "buying-channel-assistant": ("start",),
        "fast-track-questionnaire-assistant": ("start", "questionnaire_exists", "knowledge_agent"),
        "gifts-hospitality-onboarding-buying": ("start",),
        "other-payee-onboarding-assitant": ("start",),
        "quick-pay-assistant": ("start", "rule_0", "knowledge_agent"),
        "supplier-recommendation-assistant": ("start",),
        "v3-category-specific-questionnaire": (
            "start",
            "questionnaire_exists_check",
            "getcategoryspecificquestionnaire",
        ),
    }
)


def _sub_agent(name: str, prefix: tuple[str, ...]) -> Scenario:
    blueprint = platform.plan(f"platform/{name}")
    loop = platform.agent_loops(blueprint)[0]
    agent = loop.agent.name
    return Scenario(
        name=f"platform-{name}",
        fixture=f"platform/{name}",
        turns=(
            ScenarioTurn(
                text="Create a request",
                script=[],
                path=(*prefix, agent, *loop.path, agent, *loop.path, agent, "output"),
            ),
        ),
        responder=platform.loop_responder(loop, platform.structured_names(blueprint)),
        server_tools=PLATFORM_TOOLS,
        sandbox=True,
        notes=f"F-18, B-13: {agent} routes through {loop.tool.call_name} twice, then answers.",
    )


def _orchestrator() -> Scenario:
    fixture = "platform/intelligent-buying-orchestrator"
    return Scenario(
        name="platform-intelligent-buying-orchestrator",
        fixture=fixture,
        children=("platform/quick-pay-assistant",),
        turns=(
            ScenarioTurn(
                text="I need to pay a supplier quickly",
                script=[],
                path=(
                    "start",
                    "channel_lockin",
                    "get_user_context",
                    "extract_entities",
                    "prerouting_rules",
                    "procurement_policy_assistant",
                    "current_state_router",
                    *QUICK_PAY,
                    "display_output_rule",
                    "output",
                ),
            ),
            ScenarioTurn(
                text="The amount is 120",
                script=[],
                path=("start", "channel_lockin", *QUICK_PAY, "display_output_rule", "output"),
            ),
        ),
        responder=platform.orchestrator_responder(platform.structured_names(platform.plan(fixture))),
        server_tools=PLATFORM_TOOLS,
        sandbox=True,
        notes="F-17, B-12: routes on flow.current_state; the saved state locks the channel on turn 2.",
    )


def _catalogue() -> Scenario:
    fixture = "platform/catalogue-assistant"
    return Scenario(
        name="platform-catalogue-assistant",
        fixture=fixture,
        turns=(
            ScenarioTurn(
                text="I want a laptop",
                script=[],
                path=(
                    "start",
                    "set_pv_data",
                    "supervisor",
                    "set_placeholder",
                    "navigator",
                    "ask_to_clarify",
                    "output",
                ),
            ),
        ),
        responder=platform.plain_responder(
            platform.structured_names(platform.plan(fixture)),
            "Which item do you need?",
            {"nextStep": "clarify"},
        ),
        server_tools=PLATFORM_TOOLS,
        sandbox=True,
        notes="No agent loop: the supervisor asks to clarify; the script nodes run in the sandbox.",
    )


PLATFORM: tuple[Scenario, ...] = (
    _orchestrator(),
    _catalogue(),
    *(_sub_agent(name, prefix) for name, prefix in SUB_AGENTS.items()),
)


def transfer(member: str, message: str) -> Turn:
    """A Swarm member's handoff (P4-01)."""
    return Turn.call(f"transfer_to_{member}", {"message": message})


#: The Facility Operations scope the request parser returns (the Node.js scenarios' scope).
FACILITY_SCOPE = {
    "intent": "investigation",
    "facilityName": "Baruch Houses",
    "utilityType": "Electricity",
    "periodStart": "2024-01-01",
    "periodEnd": "2024-03-31",
    "includeRisk": False,
    "referencePeriodStart": "2023-01-01",
    "referencePeriodEnd": "2023-03-31",
}
ALLOWED = Turn.structured("GuardrailDecision", {"allowed": True, "reason": "a facility question"})


def investigation(report: str) -> list[Turn]:
    """Guardrail, parser, then coordinator -> energy_analyst (one tool call) -> report_writer."""
    return [
        ALLOWED,
        Turn.say(json.dumps(FACILITY_SCOPE)),
        transfer("energy_analyst", "Cost and consumption for Baruch Houses, Q1 2024 against Q1 2023"),
        Turn.call("get_utility_bills_with_historical_data", {}),
        transfer("report_writer", "Costs up 12 percent; no data gaps"),
        Turn.say(report),
    ]


FACILITY_INVESTIGATION = (
    "Start_1",
    "input_guardrail",
    "request_parser",
    "scope_router",
    "weather_context",
    "operations_team",
    "score_findings",
    "review_gate",
)
SWARMS: tuple[Scenario, ...] = (
    Scenario(
        name="customer-support-swarm",
        fixture="templates/customer_support_swarm",
        turns=(
            ScenarioTurn(
                text="I was charged twice for invoice 7",
                script=[
                    transfer("billing", "Duplicate charge on invoice 7"),
                    Turn.say("We refunded the duplicate charge."),
                ],
                path=("Start_1", "support_team", "Output_1"),
            ),
        ),
        notes="F-11: triage hands off to billing, which answers.",
    ),
    Scenario(
        name="facility-operations-swarm",
        fixture="templates/facility_operations_swarm",
        turns=(
            ScenarioTurn(
                text="Why did electricity costs rise at Baruch Houses in Q1 2024?",
                script=investigation("# Report\nCosts rose 12 percent.\nSEVERITY: 3"),
                path=(*FACILITY_INVESTIGATION, "record_run", "report_view", "Report"),
            ),
            ScenarioTurn(
                text="And the heating bills in Q1 2024?",
                script=investigation("# Report\nCosts doubled.\nSEVERITY: 8"),
                path=(*FACILITY_INVESTIGATION, "publish_approval", "record_run", "report_view", "Report"),
                answers=({"approved": True, "comment": "Publish it."},),
            ),
            ScenarioTurn(
                text="And the water bills in Q1 2024?",
                script=investigation("# Report\nCosts doubled.\nSEVERITY: 8"),
                path=(*FACILITY_INVESTIGATION, "publish_approval", "reviewer_feedback", "Rejected"),
                answers=({"approved": False, "comment": "Too vague."}, {"value": "Name the meters."}),
            ),
            ScenarioTurn(
                text="And gas in Q1 2024?",
                script=investigation("# Report\nNo severity line here."),
                path=(*FACILITY_INVESTIGATION, "publish_approval", "record_run", "report_view", "Report"),
                answers=({"approved": True},),
            ),
            ScenarioTurn(
                text="What is an HCF?",
                script=[
                    ALLOWED,
                    Turn.say(json.dumps({**FACILITY_SCOPE, "intent": "definition"})),
                    Turn.say("A hundred cubic feet."),
                ],
                path=(
                    "Start_1",
                    "input_guardrail",
                    "request_parser",
                    "scope_router",
                    "quick_answer",
                    "Quick_answer",
                ),
            ),
            ScenarioTurn(
                text="Drop the bills table",
                script=[
                    Turn.structured("GuardrailDecision", {"allowed": False, "reason": "asks to change data"})
                ],
                path=("Start_1", "input_guardrail", "Refused"),
            ),
        ),
        mcp=MappingProxyType({"get_utility_bills_with_historical_data": ok(None, [{"expense": 1200}])}),
        recordings=("facility_operations_swarm",),
        sandbox=True,
        notes=(
            "F-12, F-13 (Section 20.5): SEVERITY 3 publishes without review; SEVERITY 8 pauses at "
            "publish_approval and resumes approved, or rejected into reviewer_feedback (Human Input); "
            "a report without a SEVERITY line is reviewed; a definition takes the quick answer; a blocked "
            "request is refused."
        ),
    ),
    Scenario(
        name="approval-flow",
        fixture="templates/approval_flow",
        turns=(
            ScenarioTurn(
                text="Please refund order 1042",
                script=[Turn.say("Dear customer, your refund for order 1042 is on its way.")],
                path=("Start_1", "LLM_1", "Approval_1", "Approved"),
                answers=({"approved": True},),
            ),
            ScenarioTurn(
                text="Please refund order 1043",
                script=[Turn.say("Dear customer, we cannot refund order 1043.")],
                path=("Start_1", "LLM_1", "Approval_1", "Rejected"),
                answers=({"approved": False, "comment": "Check the order first."},),
            ),
        ),
        notes="F-13: Approval_1 pauses the run; each answer resumes it into Approved or Rejected.",
    ),
)

SCENARIOS: tuple[Scenario, ...] = (
    Scenario(
        name="question-answering",
        fixture="templates/question_answering_with_tools",
        turns=(QUESTION, CALCULATED),
        notes="One answer, then a calculator call (a built-in tool, P3-02).",
    ),
    *(
        Scenario(
            name=f"test-agent-workflow-{dialect}",
            fixture=f"{dialect}/test-agent-workflow",
            turns=(
                ScenarioTurn(
                    text="Hello there",
                    script=[Turn.say("Hello! How can I help?")],
                    path=("Start_1", "Agent_1", "Output_1"),
                ),
                ScenarioTurn(
                    text="And again",
                    script=[Turn.say("Still here.")],
                    path=("Start_1", "Agent_1", "Output_1"),
                ),
            ),
            unreachable=("DBQuery_1",),
        )
        for dialect in ("zonic", "zonic-versioned")
    ),
    Scenario(name="new-workflow", fixture="invalid/new-workflow", refused="PLAN_INVALID"),
    *(
        Scenario(
            name=f"fsh-energy-agent-{folder}",
            fixture=f"{folder}/fsh-energy-agent",
            turns=(ENERGY_TURN,),
            mcp=ENERGY_MCP,
            notes="F-08: Rule_1 takes the analysisType branch; utility-analysis (5 incoming) runs once.",
        )
        for folder in ("open-data", "zonic", "zonic-versioned")
    ),
    *(
        Scenario(
            name=f"fsh-facility-agent-v2-{dialect}",
            fixture=f"{dialect}/fsh-facility-agent-v2",
            turns=(
                ScenarioTurn(
                    text="Which facilities had boiler alarms?",
                    script=[
                        Turn.say('{"keywords": ["boiler", "alarm"]}'),
                        Turn.call("handoff_to", {"target": "direct-answer", "reason": "general question"}),
                        Turn.say("<p>Three facilities reported boiler alarms.</p>"),
                    ],
                    path=(
                        "Start_1",
                        "keywords-extraction",
                        "facility-agent",
                        "direct-answer",
                        "View_1",
                        "VarUpdate_1",
                        "Output_1",
                    ),
                ),
                ScenarioTurn(
                    text="Summarize the alarm data by site",
                    script=[
                        Turn.say('{"keywords": ["alarm", "site"]}'),
                        Turn.call("handoff_to", {"target": "data-processing", "reason": "needs data"}),
                        Turn.say("<table><tr><td>Site A</td><td>2</td></tr></table>"),
                    ],
                    path=(
                        "Start_1",
                        "keywords-extraction",
                        "facility-agent",
                        "data-processing",
                        "View_2",
                        "VarUpdate_1",
                        "Output_1",
                    ),
                ),
            ),
            notes="F-08 (routing by handoff, the join at VarUpdate_1); views are html_template.",
        )
        for dialect in ("zonic", "zonic-versioned")
    ),
    *(
        Scenario(
            name=f"test-relationship-{dialect}",
            fixture=f"{dialect}/test-relationship",
            children=(f"{dialect}/fsh-energy-agent",),
            turns=(
                ScenarioTurn(
                    text=ENERGY_TURN.text,
                    script=[Turn.call("handoff_to", {"target": "energy-agent"}), *ENERGY_TURN.script],
                    path=(
                        "Start_1",
                        "Agent_1",
                        "energy-agent",
                        *(f"energy-agent/{name}" for name in ENERGY_TURN.path),
                        "Output_1",
                    ),
                ),
                ScenarioTurn(
                    text="Thanks, that is all.",
                    script=[Turn.say("You are welcome.")],
                    path=("Start_1", "Agent_1", "Output_1"),
                ),
            ),
            mcp=ENERGY_MCP,
            notes="F-10: the agent hands off into the fsh-energy-agent sub-flow; no handoff goes to Output_1.",
        )
        for dialect in ("zonic", "zonic-versioned")
    ),
    Scenario(
        name="fsh-facility-agent-open-data",
        fixture="open-data/fsh-facility-agent",
        turns=(analysis_turn("facility-agent"), sql_turn("facility-agent", "facility-query")),
        recordings=("fsh-facility-agent",),
        notes="F-08, F-09: useDBQuery unset takes r_else, the NYC Open Data call (recorded).",
    ),
    Scenario(
        name="fsh-risk-agent-open-data",
        fixture="open-data/fsh-risk-agent",
        turns=(analysis_turn("risk-agent"), sql_turn("risk-agent", "query-analysis")),
        recordings=("fsh-risk-agent",),
        notes='F-08, F-09, N-13: useDBQuery "true" (a string) matches the rule; the DB Query is recorded.',
    ),
    *(
        Scenario(
            name=f"fsh-facility-agent-{dialect}",
            fixture=f"{dialect}/fsh-facility-agent",
            turns=(analysis_turn("facility-agent"),),
            notes="A Zonic export's HTTP node has no URL, so only the analysis branch runs here.",
        )
        for dialect in ("zonic", "zonic-versioned")
    ),
    *(
        Scenario(
            name=f"fsh-risk-agent-{dialect}",
            fixture=f"{dialect}/fsh-risk-agent",
            turns=(analysis_turn("risk-agent"), sql_turn("risk-agent", "query-analysis")),
            mcp=FACILITY_ROWS,
            notes="N-13: the T-SQL DB Query through MCP execute.",
        )
        for dialect in ("zonic", "zonic-versioned")
    ),
    *(
        Scenario(
            name=f"{folder.replace('/', '-')}-supervisor",
            fixture=folder,
            children=children,
            turns=(
                ScenarioTurn(
                    text="Hello",
                    script=[Turn.say("I can answer that myself.")],
                    path=("Start_1", "supervisor-agent", "Output_1"),
                ),
                ScenarioTurn(
                    text="Which facilities are over budget?",
                    script=[
                        Turn.call("handoff_to", {"target": "risk-energy"}),
                        *analysis_turn("risk-agent").script,
                    ],
                    path=(
                        "Start_1",
                        "supervisor-agent",
                        "risk-energy",
                        *(f"risk-energy/{name}" for name in analysis_turn("risk-agent").path),
                        "Output_1",
                    ),
                ),
            ),
            notes="F-10: the supervisor answers itself, then hands off into the fsh-risk-agent sub-flow.",
        )
        for folder, children in (
            *(
                (
                    f"{d}/all-agents",
                    (f"{d}/fsh-energy-agent", f"{d}/fsh-facility-agent-v2", f"{d}/fsh-risk-agent"),
                )
                for d in ("zonic", "zonic-versioned")
            ),
            (
                "django/complex-agent",
                ("zonic/fsh-energy-agent", "zonic/fsh-facility-agent", "zonic/fsh-risk-agent"),
            ),
        )
    ),
    *PLATFORM,
    *SWARMS,
)
