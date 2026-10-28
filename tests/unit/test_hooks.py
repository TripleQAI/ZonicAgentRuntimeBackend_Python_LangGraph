"""The hook framework and the core hook types (P2-06: K-01 to K-07, K-09, K-10, K-12, K-15, K-16)."""

from __future__ import annotations

import asyncio
import itertools
from dataclasses import replace
from typing import Any, ClassVar

import pytest
from langchain_core.tools import tool
from pydantic import BaseModel

from tests.fakes.scripted_chat_model import Turn
from tests.kinds.agents import agent_harness
from tests.kinds.harness import DEFAULT_INPUT, EchoKind, FailingKind, Harness, Recorder
from tests.kinds.streaming import DriverHarness
from zonic_runtime.config.env import EnvironmentModel
from zonic_runtime.config.settings import RuntimeResources, load_resources
from zonic_runtime.engine.node_template import NodeServices
from zonic_runtime.errors import ZonicError
from zonic_runtime.hooks.builtin import builtin_hooks
from zonic_runtime.hooks.node_hooks import TemplateHooks
from zonic_runtime.hooks.registry import HookRegistry
from zonic_runtime.hooks.resolver import HookResolver
from zonic_runtime.hooks.runner import HookRunner
from zonic_runtime.hooks.types import BaseHook, HookContext
from zonic_runtime.models.hooks.binding import HookBindings
from zonic_runtime.models.hooks.result import HookPatch, HookResult

DEV = load_resources(EnvironmentModel(env="dev"))
CALLS: list[tuple[str, str | None, str]] = []


class Config(BaseModel):
    label: str = ""
    seconds: float = 0.0
    action: str = "continue"
    message: str | None = None


class RecordHook(BaseHook):
    """Records where it ran; optionally waits, or returns a fixed action."""

    name: ClassVar[str] = "record"
    config: ClassVar[type[BaseModel]] = Config

    async def run(self, context: HookContext, config: Any) -> HookResult:
        CALLS.append((context.point, context.spec.node_id if context.spec else None, config.label))
        if config.seconds:
            await asyncio.sleep(config.seconds)
        return HookResult(action=config.action, message=config.message)


class RetryModelHook(BaseHook):
    """``afterModelCall``: retries the first answer once."""

    name: ClassVar[str] = "retryModel"
    config: ClassVar[type[BaseModel]] = Config

    async def run(self, context: HookContext, config: Any) -> HookResult:
        return HookResult(action="retry") if context.call.get("text") == "first draft" else HookResult()


def resources(**hooks: Any) -> RuntimeResources:
    settings = (
        DEV.settings.model_copy(update={"hooks": DEV.settings.hooks.model_copy(update=hooks)})
        if hooks
        else DEV.settings
    )
    return replace(DEV, settings=settings)


def template_hooks(bindings: list[dict[str, Any]], base: RuntimeResources = DEV) -> TemplateHooks:
    services = NodeServices.from_resources(base)
    registry = HookRegistry([*builtin_hooks(services.schemas), RecordHook(), RetryModelHook()])
    parsed = HookBindings.model_validate({"bindings": bindings})
    resolver = HookResolver(registry, parsed, base.settings.hooks)
    return TemplateHooks(HookRunner(resolver, base.settings.hooks), services)


def harness(
    bindings: list[dict[str, Any]], kinds: dict[str, Any] | None = None, base: RuntimeResources = DEV
) -> Harness:
    recorder = Recorder()
    factories = {"variable": lambda s, b: EchoKind(s, b, recorder), **(kinds or {})}
    return Harness(base, factories, recorder=recorder, hooks=template_hooks(bindings, base))


def binding(id_: str, on: str | list[str], hook: str = "record", **fields: Any) -> dict[str, Any]:
    return {"id": id_, "hook": hook, "on": [on] if isinstance(on, str) else on, **fields}


def node(node_id: str, type_: str = "variable", **properties: Any) -> dict[str, Any]:
    return {
        "id": node_id,
        "type": type_,
        "name": node_id,
        "config": {},
        **({"properties": properties} if properties else {}),
    }


def chain(*nodes: dict[str, Any], **document: Any) -> dict[str, Any]:
    ids = ["start", *(n["id"] for n in nodes)]
    return {
        "schemaVersion": "2.0",
        "nodes": [{"id": "start", "type": "start", "config": {}}, *nodes],
        "edges": [{"source": a, "target": b} for a, b in itertools.pairwise(ids)],
        **document,
    }


@pytest.fixture(autouse=True)
def clear_calls() -> None:
    CALLS.clear()


def calls(point: str = "beforeNode") -> list[tuple[str | None, str]]:
    return [(n, label) for p, n, label in CALLS if p == point]


# --- K-01, K-02, K-03 ---------------------------------------------------------------------------


async def test_K01_matching_by_kind_type_name_workflow_and_tag() -> None:
    bindings = [
        binding("kinds", "beforeNode", match={"nodeKinds": ["variable"]}, config={"label": "kind"}),
        binding("types", "beforeNode", match={"nodeTypes": ["start"]}, config={"label": "type"}),
        binding("names", "beforeNode", match={"nodeNames": ["b"]}, config={"label": "name"}),
        binding("tags", "beforeNode", match={"tags": ["pii"]}, config={"label": "tag"}),
        binding(
            "both",
            "beforeNode",
            match={"nodeKinds": ["variable"], "nodeNames": ["a"]},
            config={"label": "both"},
        ),
        binding(
            "workflow",
            "beforeNode",
            match={"workflowIds": ["wf"], "nodeNames": ["c"]},
            config={"label": "wf"},
        ),
        binding("elsewhere", "beforeNode", match={"workflowIds": ["other"]}, config={"label": "never"}),
    ]
    await harness(bindings).run(chain(node("a"), node("b", tags=["pii", "x"]), node("c")))
    assert sorted(calls()) == sorted(
        [
            ("start", "type"),
            ("a", "kind"),
            ("a", "both"),
            ("b", "kind"),
            ("b", "name"),
            ("b", "tag"),
            ("c", "kind"),
            ("c", "wf"),
        ]
    )


@pytest.mark.parametrize(("value", "ran"), [(1, True), (2, False)])
async def test_K02_when_false_means_not_run(value: int, ran: bool) -> None:
    when = {"conditionLogic": "AND", "conditions": [{"field": "{{flow.x}}", "op": "eq", "value": 1}]}
    h = harness([binding("w", "beforeNode", match={"nodeNames": ["a"]}, when=when)])
    inputs = {**DEFAULT_INPUT, "flow": {"values": {"x": value}, "provenance": {}, "conflicts": []}}
    await h.run(chain(node("a")), inputs)
    assert (calls() == [("a", "")]) is ran


async def test_K03_global_workflow_and_node_bindings_run_by_order_then_source() -> None:
    base = resources(document_allowed_types=[*DEV.settings.hooks.document_allowed_types, "record"])
    global_ = [
        binding("g1", "beforeNode", match={"nodeNames": ["a"]}, order=20, config={"label": "global-20"}),
        binding("g2", "beforeNode", match={"nodeNames": ["a"]}, order=10, config={"label": "global-10"}),
    ]
    workflow = [
        binding("w1", "beforeNode", match={"nodeNames": ["a"]}, order=10, config={"label": "workflow-10"})
    ]
    own = [
        binding("n1", "beforeNode", order=10, config={"label": "node-10"}),
        binding("n2", "beforeNode", order=5, config={"label": "node-5"}),
    ]
    await harness(global_, base=base).run(chain(node("a", hooks=own), runtimeHooks=workflow))
    assert [label for _, label in calls()] == ["node-5", "global-10", "workflow-10", "node-10", "global-20"]


# --- K-04 ------------------------------------------------------------------------------------


def transform(path: str, value: Any, on: str = "beforeNode", **fields: Any) -> dict[str, Any]:
    return binding(
        f"t-{path}",
        on,
        hook="transform",
        match={"nodeNames": ["n"]},
        config={"set": [{"path": path, "value": value}]},
        **fields,
    )


def llm_node() -> dict[str, Any]:
    return {
        "id": "n",
        "type": "llm",
        "name": "n",
        "config": {"messages": [{"role": "user", "content": "hi"}]},
    }


async def test_K04_before_node_changes_the_input_and_the_kind_sees_it() -> None:
    recorder = Recorder()
    h = harness(
        [transform("input.prompt", "{{system.userQuery}} (rewritten)")],
        {"llm": lambda s, b: EchoKind(s, b, recorder)},
    )
    h.recorder = recorder
    final, *_ = await h.run(chain(llm_node()))
    assert recorder.inputs["n"]["prompt"] == "hello (rewritten)"
    assert final["nodes"]["n"]["status"] == "succeeded"


async def test_K04_an_invalid_input_change_is_hook_result_invalid() -> None:
    h = harness([transform("input.messages", "not a list")], {"llm": lambda s, b: EchoKind(s, b, Recorder())})
    with pytest.raises(ZonicError, match="HOOK_RESULT_INVALID"):
        await h.run(chain(llm_node()))


async def test_K04_skip_completes_the_node_with_the_hooks_output_and_routing() -> None:
    class Skip(RecordHook):
        name: ClassVar[str] = "skipper"

        async def run(self, context: HookContext, config: Any) -> HookResult:
            return HookResult(action="skip", patch=HookPatch(output={"cached": True}), handles=["default"])

    recorder = Recorder()
    services = NodeServices.from_resources(DEV)
    registry = HookRegistry([*builtin_hooks(services.schemas), Skip()])
    bindings = HookBindings.model_validate(
        {"bindings": [binding("s", "beforeNode", hook="skipper", match={"nodeNames": ["a"]})]}
    )
    hooks = TemplateHooks(
        HookRunner(HookResolver(registry, bindings, DEV.settings.hooks), DEV.settings.hooks), services
    )
    h = Harness(DEV, {"variable": lambda s, b: EchoKind(s, b, recorder)}, recorder=recorder, hooks=hooks)
    final, *_ = await h.run(chain(node("a"), node("b")))
    assert "a" not in recorder.calls and "b" in recorder.calls
    assert final["nodes"]["a"]["output"] == {"cached": True}


async def test_K04_fail_fails_the_node_with_the_hooks_code() -> None:
    class Fail(RecordHook):
        name: ClassVar[str] = "failer"

        async def run(self, context: HookContext, config: Any) -> HookResult:
            return HookResult(action="fail", code="INPUT_INVALID")

    services = NodeServices.from_resources(DEV)
    registry = HookRegistry([Fail()])
    bindings = HookBindings.model_validate(
        {"bindings": [binding("f", "beforeNode", hook="failer", match={"nodeNames": ["a"]})]}
    )
    hooks = TemplateHooks(
        HookRunner(HookResolver(registry, bindings, DEV.settings.hooks), DEV.settings.hooks), services
    )
    recorder = Recorder()
    h = Harness(DEV, {"variable": lambda s, b: EchoKind(s, b, recorder)}, recorder=recorder, hooks=hooks)
    with pytest.raises(ZonicError, match="INPUT_INVALID"):
        await h.run(chain(node("a")))
    assert recorder.calls == []


# --- K-05 ------------------------------------------------------------------------------------


def routed_document() -> dict[str, Any]:
    rule = {
        "id": "n",
        "type": "rule",
        "name": "n",
        "config": {
            "rules": [
                {"ruleId": "r_a", "conditions": [{"field": "1", "op": "eq", "value": "1"}]},
                {"ruleId": "r_else", "isDefault": True},
            ]
        },
    }
    return {
        "schemaVersion": "2.0",
        "nodes": [{"id": "start", "type": "start", "config": {}}, rule, node("a"), node("b")],
        "edges": [
            {"source": "start", "target": "n"},
            {"source": "n", "target": "a", "sourceHandle": "r_a"},
            {"source": "n", "target": "b", "sourceHandle": "r_else"},
        ],
    }


async def test_K05_after_node_transforms_the_output_and_later_steps_see_it() -> None:
    later = node("b")
    later["config"] = {"updates": [{"path": "flow.seen", "value": "{{nodes.a.output.extra}}"}]}
    bindings = [
        binding(
            "t",
            "afterNode",
            hook="transform",
            match={"nodeNames": ["a"]},
            config={"set": [{"path": "output.extra", "value": "added"}]},
        )
    ]
    from zonic_runtime.engine.kinds.flow_kinds import VariableKind

    final, *_ = await harness(bindings, {"variable": VariableKind}).run(chain(node("a"), later))
    assert final["nodes"]["a"]["output"]["extra"] == "added" and final["flow"]["values"]["seen"] == "added"


class Route(RecordHook):
    name: ClassVar[str] = "router"

    async def run(self, context: HookContext, config: Any) -> HookResult:
        return HookResult(handles=[config.label])


def route_harness(handle: str, can_route: bool) -> Harness:
    from zonic_runtime.engine.kinds.flow_kinds import RouterKind

    services = NodeServices.from_resources(DEV)
    raw = binding(
        "r",
        "afterNode",
        hook="router",
        match={"nodeNames": ["n"]},
        canRoute=can_route,
        config={"label": handle},
    )
    resolver = HookResolver(
        HookRegistry([Route()]), HookBindings.model_validate({"bindings": [raw]}), DEV.settings.hooks
    )
    recorder = Recorder()
    kinds = {"router": RouterKind, "variable": lambda s, b: EchoKind(s, b, recorder)}
    return Harness(
        DEV, kinds, recorder=recorder, hooks=TemplateHooks(HookRunner(resolver, DEV.settings.hooks), services)
    )


async def test_K05_routing_changes_need_can_route_and_an_existing_handle() -> None:
    final, *_ = await route_harness("r_else", True).run(routed_document())
    assert "b" in final["nodes"] and "a" not in final["nodes"]
    with pytest.raises(ZonicError, match="HOOK_RESULT_INVALID"):
        await route_harness("r_else", False).run(routed_document())
    with pytest.raises(ZonicError, match="HOOK_RESULT_INVALID"):
        await route_harness("r_missing", True).run(routed_document())


# --- K-06 ------------------------------------------------------------------------------------


async def test_K06_on_node_error_retries_up_to_the_bound_then_the_policy_applies() -> None:
    recorder = Recorder()
    bindings = [
        binding(
            "retry",
            "onNodeError",
            hook="retry",
            match={"nodeNames": ["a"]},
            config={"attempts": 5, "codes": ["TOOL_UNAVAILABLE"]},
        )
    ]
    hooks = template_hooks(bindings, resources(max_retries_per_node=2))
    kinds = {"variable": lambda s, b: FailingKind(s, b, recorder, "TOOL_UNAVAILABLE", failures=2)}
    final, *_ = await Harness(resources(max_retries_per_node=2), kinds, recorder=recorder, hooks=hooks).run(
        chain(node("a"))
    )
    assert (
        recorder.attempts["a"] == 3 and final["nodes"]["a"]["status"] == "succeeded" and final["errors"] == []
    )
    recorder = Recorder()
    kinds = {"variable": lambda s, b: FailingKind(s, b, recorder, "TOOL_UNAVAILABLE", failures=10)}
    with pytest.raises(ZonicError, match="TOOL_UNAVAILABLE"):
        await Harness(resources(max_retries_per_node=2), kinds, recorder=recorder, hooks=hooks).run(
            chain(node("a"))
        )
    assert recorder.attempts["a"] == 3  # the first visit and two hook retries


async def test_K06_fallback_recovers_with_its_output_and_handles() -> None:
    bindings = [
        binding(
            "fb",
            "onNodeError",
            hook="fallback",
            match={"nodeNames": ["a"]},
            config={"output": {"text": "sorry, {{system.userQuery}}"}, "activate": ["default"]},
        )
    ]
    recorder = Recorder()
    kinds = {
        "variable": lambda s, b: (
            FailingKind(s, b, recorder, "TOOL_UNAVAILABLE") if s.node_id == "a" else EchoKind(s, b, recorder)
        )
    }
    final, *_ = await Harness(DEV, kinds, recorder=recorder, hooks=template_hooks(bindings)).run(
        chain(node("a"), node("b"))
    )
    assert final["nodes"]["a"]["output"] == {"text": "sorry, hello"} and "b" in recorder.calls


# --- K-07 ------------------------------------------------------------------------------------


@pytest.mark.parametrize("policy", ["ignore", "fail_node", "fail_run"])
async def test_K07_a_hook_timeout_follows_its_failure_policy(policy: str) -> None:
    slow = binding(
        "slow",
        "beforeNode",
        match={"nodeNames": ["a"]},
        timeoutMs=20,
        failurePolicy=policy,
        config={"seconds": 1},
    )
    document = chain(node("a"), node("b"))
    document["nodes"][1]["config"] = {"errorHandling": {"policy": "continue"}}
    h = harness([slow])
    if policy == "fail_run":
        with pytest.raises(ZonicError, match="HOOK_TIMEOUT"):
            await h.run(document)
        return
    final, *_ = await h.run(document)
    if policy == "ignore":
        assert final["nodes"]["a"]["status"] == "succeeded"
        assert [(w["code"], w["cause"]) for w in final["warnings"]] == [("HOOK_IGNORED", "HOOK_TIMEOUT")]
    else:  # the node failed with HOOK_TIMEOUT; its own policy (continue) applied
        assert final["nodes"]["a"]["status"] == "failed" and [e["code"] for e in final["errors"]] == [
            "HOOK_TIMEOUT"
        ]
        assert "b" in final["nodes"]


# --- K-09, K-10, K-15, K-16 --------------------------------------------------------------------


async def test_K09_a_node_cannot_turn_off_a_non_overridable_binding() -> None:
    bindings = [
        binding("audit", "beforeNode", overridable=False, config={"label": "audit"}),
        binding("soft", "beforeNode", config={"label": "soft"}),
    ]
    final, *_ = await harness(bindings).run(chain(node("a", disabledHooks=["audit", "soft"]), node("b")))
    assert ("a", "audit") in calls() and ("a", "soft") not in calls() and ("b", "soft") in calls()
    assert {"code": "HOOK_NOT_OVERRIDABLE", "binding": "audit", "nodeId": "a"} in [
        {k: v for k, v in w.items() if k != "_seq"} for w in final["warnings"]
    ]


@pytest.mark.parametrize(
    ("hooks", "path"),
    [
        ([binding("x", "beforeNode")], "runtimeHooks[0].hook"),
        (
            [binding("x", "beforeNode", hook="transform", config={"set": "nope"})],
            "runtimeHooks[0].config.set",
        ),
        ([{"id": "x", "hook": "transform"}], "runtimeHooks[0].on"),
    ],
    ids=["type-not-allowed", "bad-config", "bad-binding"],
)
async def test_K10_a_document_binding_that_is_not_allowed_or_not_valid_is_document_invalid(
    hooks: list[dict[str, Any]], path: str
) -> None:
    h = harness([])
    blueprint = h.plan(chain(node("a"), runtimeHooks=hooks))
    with pytest.raises(ZonicError) as caught:
        h.hooks.runner.resolver.of(blueprint)
    assert caught.value.code == "DOCUMENT_INVALID"
    assert any(i["path"] == path for i in caught.value.details["issues"])


@pytest.mark.parametrize(
    ("raw", "path"),
    [
        (binding("x", "beforeNode", hook="nope"), "hooks.json.bindings[0].hook"),
        (
            binding("x", "beforeNode", hook="retry", config={"attempts": 0}),
            "hooks.json.bindings[0].config.attempts",
        ),
        (
            binding("x", "beforeNode", hook="validate", config={"model": "zonic.nothing@9"}),
            "hooks.json.bindings[0].config",
        ),
    ],
    ids=["unknown-type", "bad-config", "unknown-schema"],
)
def test_K15_an_invalid_hooks_json_stops_start_up_with_the_path(raw: dict[str, Any], path: str) -> None:
    with pytest.raises(ZonicError) as caught:
        template_hooks([raw])
    assert caught.value.code == "RESOURCE_INVALID" and caught.value.details["file"] == "hooks.json"
    assert [i["path"] for i in caught.value.details["issues"]] == [path]
    with pytest.raises(ValueError):
        HookBindings.model_validate({"bindings": [{"id": "bad id!", "hook": "x", "on": ["beforeNode"]}]})


async def test_K16_a_new_hook_type_runs_by_configuration_alone() -> None:
    from zonic_runtime.application.runtime import WorkflowRuntime

    class Stamp(RecordHook):
        name: ClassVar[str] = "stamp"

        async def run(self, context: HookContext, config: Any) -> HookResult:
            return HookResult(
                updates=[{"path": "flow.stamped", "value": context.spec.node_id if context.spec else ""}]
            )  # type: ignore[list-item]

    base = replace(
        DEV,
        hooks=HookBindings.model_validate(
            {"bindings": [binding("s", "afterNode", hook="stamp", match={"nodeNames": ["a"]})]}
        ),
    )
    runtime = WorkflowRuntime(base, rows=None, hook_types=[Stamp()])  # type: ignore[arg-type]
    recorder = Recorder()
    h = Harness(
        base, {"variable": lambda s, b: EchoKind(s, b, recorder)}, recorder=recorder, hooks=runtime.hooks
    )
    final, *_ = await h.run(chain(node("a")))
    assert final["flow"]["values"]["stamped"] == "a"


# --- K-12 ------------------------------------------------------------------------------------


@tool
def lookup(city: str) -> str:
    """Look up the weather."""
    return "sunny"


async def test_K12_before_tool_call_cancels_and_after_model_call_retries() -> None:
    bindings = [
        binding(
            "cancel",
            "beforeToolCall",
            match={"nodeNames": ["ag"]},
            config={"action": "cancel", "message": "Lookups are paused."},
        ),
        binding("again", "afterModelCall", hook="retryModel", match={"nodeNames": ["ag"]}),
    ]
    script = [Turn.call("lookup", {"city": "Oslo"}), Turn.say("first draft"), Turn.say("second draft")]
    h, models = agent_harness(script, DEV, tools={"ag": [lookup]})
    h.hooks = template_hooks(bindings)
    agent = {"id": "ag", "type": "agent", "name": "ag", "config": {"systemPrompt": "Help."}}
    final, *_ = await h.run(chain(agent))
    second = models.model.requests[1].messages
    assert second[-1].type == "tool" and second[-1].text == "Lookups are paused."
    assert len(models.model.requests) == 3 and final["nodes"]["ag"]["output"]["text"] == "second draft"


# --- run-level points -----------------------------------------------------------------------------


async def test_run_level_hooks_set_variables_before_and_transform_the_result_after() -> None:
    from tests.kinds.harness import ResultKind

    bindings = [
        binding(
            "vars",
            "beforeRun",
            hook="setVariables",
            config={"updates": [{"path": "flow.region", "value": "east"}]},
        ),
        binding(
            "shape",
            "afterRun",
            hook="transform",
            config={"set": [{"path": "result.region", "value": "{{flow.region}}"}]},
        ),
    ]
    recorder = Recorder()
    h = Harness(
        DEV,
        {"output": lambda s, b: ResultKind(s, b, recorder)},
        recorder=recorder,
        hooks=template_hooks(bindings),
    )
    run = await DriverHarness(h).run(chain({"id": "out", "type": "output", "config": {}}))
    assert run.outcome.result == {"answer": "out", "region": "east"}
    assert run.outcome.state is not None and run.outcome.state["flow"]["values"]["region"] == "east"
