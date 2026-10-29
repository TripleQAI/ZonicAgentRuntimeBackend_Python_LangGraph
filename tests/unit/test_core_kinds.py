"""The agent layer and the core node kinds (P2-02: N-01 to N-08, N-12, N-19, M-09, B-02 to B-04,
S-09, D-13)."""

from __future__ import annotations

import itertools
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from botocore.exceptions import ClientError
from langchain_core.tools import tool

from tests.conformance.test_kinds import fast_retry
from tests.fakes.scripted_chat_model import Turn
from tests.kinds.agents import agent_harness
from tests.kinds.harness import DEFAULT_INPUT
from tests.kinds.streaming import DriverHarness
from zonic_runtime.config.env import EnvironmentModel
from zonic_runtime.config.settings import RuntimeResources, load_resources
from zonic_runtime.engine.agents.model_factory import ModelFactory, _provider_arguments
from zonic_runtime.engine.agents.structured import check, node_schema, output_schema, strategy
from zonic_runtime.engine.streaming.v2_mapper import V2EventSource
from zonic_runtime.errors import ZonicError
from zonic_runtime.models.dialects.refs import ModelRef
from zonic_runtime.models.nodes.configs import LlmNodeConfig

DEV = load_resources(EnvironmentModel(env="dev"))
USER = [{"role": "user", "content": "{{system.userQuery}}"}]
SCHEMA = {
    "type": "object",
    "properties": {"city": {"type": "string"}, "days": {"type": "integer", "minimum": 1}},
    "required": ["city", "days"],
}


def flow(**values: Any) -> dict[str, Any]:
    return {**DEFAULT_INPUT, "flow": {"values": values, "provenance": {}, "conflicts": []}}


def chain(*nodes: dict[str, Any], edges: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    ids = ["start", *(n["id"] for n in nodes)]
    links = edges if edges is not None else [{"source": a, "target": b} for a, b in itertools.pairwise(ids)]
    return {
        "schemaVersion": "2.0",
        "nodes": [{"id": "start", "type": "start", "config": {}}, *nodes],
        "edges": links,
    }


def llm(node_id: str = "n", **config: Any) -> dict[str, Any]:
    return {"id": node_id, "type": "llm", "name": node_id, "config": {"messages": USER, **config}}


def agent(node_id: str = "ag", **config: Any) -> dict[str, Any]:
    return {
        "id": node_id,
        "type": "agent",
        "name": node_id,
        "config": {"systemPrompt": "You help.", **config},
    }


def variable(node_id: str, updates: list[dict[str, Any]] | None = None, **config: Any) -> dict[str, Any]:
    return {
        "id": node_id,
        "type": "variable",
        "name": node_id,
        "config": {"updates": updates or [], **config},
    }


def output(
    node_id: str = "out", mapping: list[dict[str, Any]] | None = None, **config: Any
) -> dict[str, Any]:
    return {"id": node_id, "type": "output", "config": {"outputMapping": mapping or [], **config}}


@tool
def lookup(city: str) -> str:
    """Look up the weather in a city."""
    return f'{{"city": "{city}", "sky": "sunny"}}'


def enabled(resources: RuntimeResources, code: str) -> RuntimeResources:
    models = resources.models.model_copy(
        update={
            "models": {
                **resources.models.models,
                code: resources.models.models[code].model_copy(update={"enabled": True}),
            }
        }
    )
    return replace(resources, models=models)


# --- N-01 ------------------------------------------------------------------------------------


async def test_N01_start_applies_interface_input_defaults_and_initializes_flow() -> None:
    start = {
        "id": "start",
        "type": "start",
        "config": {
            "interfaceInputs": [{"name": "region", "default": "east"}, {"name": "user"}],
            "variableUpdates": [
                {"path": "flow.region", "value": "{{nodeOutput.region}}"},
                {"path": "flow.user", "value": "{{nodeOutput.interface.inputs.user}}"},
            ],
        },
    }
    h, _ = agent_harness([], DEV)
    document = {"schemaVersion": "2.0", "nodes": [start], "edges": []}
    final, *_ = await h.run(document, {**DEFAULT_INPUT, "input": {"user": "alice"}})
    out = final["nodes"]["start"]["output"]
    assert out["region"] == "east" and out["query"] == "hello"
    assert out["interface"]["inputs"] == {"region": "east", "user": "alice"}
    assert final["flow"]["values"] == {"region": "east", "user": "alice"}


# --- N-02, M-09 ------------------------------------------------------------------------------


async def test_N02_llm_text_streams_tokens_and_fills_output_text() -> None:
    h, _ = agent_harness([Turn.say("Plain answer here.", usage={"input_tokens": 3, "output_tokens": 4})], DEV)
    run = await DriverHarness(h).run(
        chain(llm(), output(mapping=[{"target": "a", "value": "{{nodes.n.output.text}}"}]))
    )
    assert run.outcome.result == {"a": "Plain answer here."}
    assert run.text("n") == "Plain answer here."
    usage = run.outcome.state["nodes"]["n"]["usage"]
    assert usage["inputTokens"] == 3 and usage["outputTokens"] == 4 and usage["modelCalls"] == 1


async def test_N02_llm_json_with_output_schema_gives_structured_output() -> None:
    node = llm(responseFormat="json", outputSchema=SCHEMA)
    h, models = agent_harness([Turn.structured("n", {"city": "Oslo", "days": 3})], DEV)
    final, *_ = await h.run(chain(node))
    out = final["nodes"]["n"]["output"]
    assert out["structuredOutput"] == {"city": "Oslo", "days": 3} and out["result"] == out["structuredOutput"]
    assert out["city"] == "Oslo"  # the answer's fields are output fields too
    sent = models.model.requests[0]
    assert sent.tool_names == ["n"] and sent.tools[0]["function"]["description"]


@pytest.mark.parametrize(
    ("answer", "parsed"),
    [
        ('{"city": "Oslo"}', {"city": "Oslo"}),
        ('```json\n{"city": "Oslo"}\n```', {"city": "Oslo"}),
        ('```\n{"city": "Oslo"}```', {"city": "Oslo"}),
        ("```json\nnot json\n```", None),
        ('The answer: {"city": "Oslo"}', None),
    ],
)
async def test_N02_llm_json_without_a_schema_parses_the_answer_bare_or_in_one_code_fence(
    answer: str, parsed: dict[str, Any] | None
) -> None:
    h, _ = agent_harness([Turn.say(answer)], DEV)
    final, *_ = await h.run(chain(llm(responseFormat="json")))
    out = final["nodes"]["n"]["output"]
    assert out["text"] == answer
    if parsed is None:
        assert "structuredOutput" not in out  # text, with the LLM_NOT_JSON warning
    else:
        assert out["structuredOutput"] == parsed and out["result"] == parsed


@pytest.mark.parametrize("answer", [{"city": "Oslo"}, {"city": "Oslo", "days": 0}, None])
async def test_N02_invalid_json_is_structured_output_invalid(answer: dict[str, Any] | None) -> None:
    node = llm(responseFormat="json", outputSchema=SCHEMA)
    turn = Turn.say("not json at all") if answer is None else Turn.structured("n", answer)
    h, _ = agent_harness([turn], DEV)
    with pytest.raises(ZonicError) as caught:
        await h.run(chain(node))
    assert caught.value.code == "STRUCTURED_OUTPUT_INVALID"


def test_M09_the_adapter_adds_title_and_description_and_picks_the_strategy() -> None:
    adapted = output_schema({"properties": {"x": {"type": "string"}}}, name="Extract entities!")
    assert adapted["title"] == "Extract_entities" and adapted["description"] and adapted["type"] == "object"
    platform = LlmNodeConfig.model_validate(
        {"structuredOutput": {"name": "extracted", "description": "Entities", "schema": SCHEMA}}
    )
    found = node_schema(platform, "n")
    assert found is not None and found["title"] == "extracted" and found["description"] == "Entities"
    assert (
        node_schema(LlmNodeConfig.model_validate({"outputSchema": SCHEMA}), "n") is None
    )  # text unless json
    assert type(strategy(found, native=True)).__name__ == "ProviderStrategy"
    assert type(strategy(found, native=False)).__name__ == "ToolStrategy"
    assert check({"city": "Oslo", "days": 2}, found, "n") == {"city": "Oslo", "days": 2}
    with pytest.raises(ZonicError, match="STRUCTURED_OUTPUT_INVALID"):
        check({"city": 1, "days": 2}, found, "n")


async def test_M09_an_agent_with_structured_output_rejects_an_invalid_answer() -> None:
    node = agent(structuredOutput={"name": "plan", "schema": SCHEMA})
    h, models = agent_harness([Turn.structured("plan", {"city": "Oslo", "days": "many"})], DEV)
    with pytest.raises(ZonicError, match="STRUCTURED_OUTPUT_INVALID"):
        await h.run(chain(node))
    assert "plan" in models.model.requests[0].tool_names
    h, _ = agent_harness([Turn.structured("plan", {"city": "Oslo", "days": 2})], DEV)
    final, *_ = await h.run(chain(node))
    assert final["nodes"]["ag"]["output"]["structuredOutput"] == {"city": "Oslo", "days": 2}


# --- N-03, B-03 -------------------------------------------------------------------------------


async def test_N03_parameters_and_message_templates_with_history_reach_the_model() -> None:
    node = llm(
        temperature=0.7,
        maxTokens=999_999,
        messages=[
            {"role": "system", "content": "Answer about {{flow.topic}}."},
            {"role": "user", "content": "{{system.userQuery}} please"},
        ],
    )
    h, models = agent_harness([Turn.say("ok")], DEV)
    history = {
        "messages": [{"role": "user", "content": "earlier"}, {"role": "assistant", "content": "before"}]
    }
    await h.run(chain(node), {**flow(topic="billing"), "history": history})
    ((_, arguments),) = models.built
    cap = DEV.models.models[DEV.models.default_model].parameters.max_tokens
    assert arguments["temperature"] == 0.7 and arguments["max_tokens"] == cap
    assert arguments["max_retries"] == DEV.settings.models.max_retries
    sent = models.model.requests[0].messages
    assert [(m.type, m.text) for m in sent] == [
        ("system", "Answer about billing."),
        ("human", "earlier"),
        ("ai", "before"),
        ("human", "hello please"),
    ]


async def test_B03_a_message_whose_content_is_a_thread_expands_into_it() -> None:
    node = llm(
        messages=[{"role": "system", "content": "Be brief."}, {"role": "user", "content": "{{messages}}"}]
    )
    h, models = agent_harness([Turn.say("ok")], DEV)
    history = {"messages": [{"role": "user", "content": "first"}, {"role": "assistant", "content": "second"}]}
    await h.run(chain(node), {**DEFAULT_INPUT, "history": history})
    sent = [(m.type, m.text) for m in models.model.requests[0].messages]
    assert sent[0] == ("system", "Be brief.") and ("human", "first") in sent and ("ai", "second") in sent


async def test_B03_prompt_template_system_entries_become_the_agents_system_prompt() -> None:
    node = agent(
        systemPrompt="You route {{flow.topic}}.", messages=[{"role": "system", "content": "Extra rule."}]
    )
    h, models = agent_harness([Turn.say("done")], DEV)
    await h.run(chain(node), flow(topic="billing"))
    first = models.model.requests[0].messages
    assert first[0].type == "system" and first[0].text == "You route billing.\n\nExtra rule."
    assert first[-1].type == "human" and first[-1].text == "hello"


# --- N-04, N-05, N-06 --------------------------------------------------------------------------


async def test_N04_llm_with_tools_runs_a_short_agent_loop_with_tool_events() -> None:
    script = [Turn.call("lookup", {"city": "Oslo"}), Turn.say("Sunny in Oslo.")]
    h, models = agent_harness(script, DEV, tools={"n": [lookup]})
    run = await DriverHarness(h).run(chain(llm()))
    out = run.outcome.state["nodes"]["n"]["output"]
    assert out["text"] == "Sunny in Oslo." and models.model.requests[0].tool_names == ["lookup"]
    assert [e["type"] for e in run.events if e["type"].startswith("tool.")] == [
        "tool.started",
        "tool.finished",
    ]
    assert run.text("n") == "Sunny in Oslo."


async def test_N05_agent_without_tools_answers_with_its_final_text() -> None:
    h, models = agent_harness([Turn.say("Hi, I can help.")], DEV)
    final, *_ = await h.run(chain(agent()))
    out = final["nodes"]["ag"]["output"]
    assert (
        out["text"] == "Hi, I can help."
        and out["toolCalls"] == []
        and models.model.requests[0].tool_names == []
    )
    chat = [m for m in final["turn_messages"] if m["visibility"] == "chat"]
    assert [(m["role"], m["kind"], m["textPreview"]) for m in chat] == [
        ("assistant", "answer", "Hi, I can help.")
    ]


async def test_N05_agent_tool_results_and_internal_turn_messages() -> None:
    script = [Turn.call("lookup", {"city": "Oslo"}, text="Let me check."), Turn.say("It is sunny.")]
    h, _ = agent_harness(script, DEV, tools={"ag": [lookup]})
    final, *_ = await h.run(chain(agent()))
    out = final["nodes"]["ag"]["output"]
    assert out["toolResults"] == {"lookup": {"city": "Oslo", "sky": "sunny"}}
    assert [c["name"] for c in out["toolCalls"]] == ["lookup"] and out["toolCalls"][0]["status"] == "success"
    kinds = [(m["kind"], m["visibility"]) for m in final["turn_messages"]]
    assert kinds == [("tool_call", "internal"), ("tool_result", "internal"), ("answer", "chat")]


async def test_N06_an_agent_storing_its_own_answer_in_a_variable_warns_of_nothing_missing() -> None:
    # The update names the visit's own output, which exists only once the agent has answered.
    node = {
        **agent(
            "lane",
            variableUpdates=[
                {"path": "flow.answer", "value": "{{nodes.analyst.output.text}}", "operation": "set"}
            ],
        ),
        "name": "analyst",
    }
    h, _ = agent_harness([Turn.say("42 kWh.")], DEV)
    final, events, *_ = await h.run(chain(node))
    assert final["flow"]["values"]["answer"] == "42 kWh."
    assert not [w for w in final["warnings"] if w["code"] == "TEMPLATE_MISSING"]
    assert not any(e["event"] == "warning" and e["code"] == "TEMPLATE_MISSING" for e in events)


async def test_N06_max_iterations_ends_with_the_partial_answer_and_a_warning() -> None:
    script = [Turn.call("lookup", {"city": f"C{i}"}, text=f"partial {i}") for i in range(1, 6)]
    h, models = agent_harness(script, DEV, tools={"ag": [lookup]})
    final, events, *_ = await h.run(chain(agent(maxIterations=2)))
    out = final["nodes"]["ag"]["output"]
    assert len(models.model.requests) == 2 and out["text"] == "partial 2" and out["stopReason"] == "limit"
    assert [w["code"] for w in final["warnings"]] == ["AGENT_TURN_LIMIT"]
    assert any(e["event"] == "warning" and e["code"] == "AGENT_TURN_LIMIT" for e in events)


# --- N-07, B-04 --------------------------------------------------------------------------------


def handoff_document(**config: Any) -> dict[str, Any]:
    nodes = [
        agent(handoffTarget=["billing", "tech"], **config),
        variable("billing"),
        variable("tech"),
        variable("after"),
    ]
    edges = [
        {"source": "start", "target": "ag"},
        {"source": "ag", "target": "billing", "sourceHandle": "handoff-billing"},
        {"source": "ag", "target": "tech", "sourceHandle": "handoff-tech"},
        {"source": "ag", "target": "after"},
    ]
    return chain(*nodes, edges=edges)


async def test_N07_a_chosen_handoff_routes_to_its_target_and_ends_the_loop() -> None:
    h, models = agent_harness(
        [Turn.call("handoff_to", {"target": "tech", "reason": "a bug"}), Turn.say("never")], DEV
    )
    final, *_ = await h.run(handoff_document())
    assert "tech" in final["nodes"] and "billing" not in final["nodes"] and "after" not in final["nodes"]
    assert final["nodes"]["ag"]["output"]["handoff"] == "tech" and models.model.remaining == 1
    schema = models.model.requests[0].tools[0]["function"]["parameters"]
    assert schema["properties"]["target"]["enum"] == ["billing", "tech"]


async def test_N07_no_handoff_follows_default() -> None:
    h, _ = agent_harness([Turn.say("I answer myself.")], DEV)
    final, *_ = await h.run(handoff_document())
    assert (
        "after" in final["nodes"]
        and "tech" not in final["nodes"]
        and "handoff" not in final["nodes"]["ag"]["output"]
    )


async def test_N07_an_invalid_target_is_rejected_and_the_model_may_choose_again() -> None:
    h, models = agent_harness(
        [Turn.call("handoff_to", {"target": "sales"}), Turn.call("handoff_to", {"target": "billing"})], DEV
    )
    final, *_ = await h.run(handoff_document())
    assert "billing" in final["nodes"]
    retry = models.model.requests[1].messages
    assert retry[-1].type == "tool" and "sales" in retry[-1].text
    h, _ = agent_harness([Turn.call("handoff_to", {"target": "sales"}), Turn.say("Fine, I answer.")], DEV)
    final, *_ = await h.run(handoff_document())
    assert "after" in final["nodes"]


async def test_B04_routing_tools_keep_their_names_and_schemas_and_record_tool_results() -> None:
    pay_schema = {"type": "object", "properties": {"amount": {"type": "number"}}, "required": ["amount"]}
    routing = [
        {
            "id": "rt1",
            "name": "pay_now",
            "target": "pay",
            "description": "Pay the invoice.",
            "schema": pay_schema,
            "parameters": {"currency": "{{flow.currency}}"},
        }
    ]
    nodes = [
        agent(routingTools=routing),
        variable("pay", [{"path": "flow.paid", "value": "{{nodes.ag.output.toolResults.pay_now.amount}}"}]),
    ]
    edges = [
        {"source": "start", "target": "ag"},
        {"source": "ag", "target": "pay", "sourceHandle": "handoff-pay"},
    ]
    h, models = agent_harness([Turn.call("pay_now", {"amount": 5}), Turn.say("never")], DEV)
    final, *_ = await h.run(chain(*nodes, edges=edges), flow(currency="EUR"))
    offered = models.model.requests[0].tools[0]["function"]
    assert offered["name"] == "pay_now" and offered["parameters"] == pay_schema
    assert final["nodes"]["ag"]["output"]["toolResults"]["pay_now"] == {"amount": 5, "currency": "EUR"}
    assert final["flow"]["values"]["paid"] == 5


# --- N-08, B-02 --------------------------------------------------------------------------------


def test_B02_a_substituted_model_is_served_by_its_stand_in_even_when_disabled() -> None:
    import json

    from tests.workflow_fixtures import read_json
    from zonic_runtime.models.resources.catalogs import ModelCatalog

    overlay = read_json(
        Path(__file__).resolve().parents[2] / "deploy" / "aws" / "resources" / "models.dev.json"
    )
    catalog = DEV.models.model_copy(update={"substitutes": overlay["substitutes"]})
    factory = ModelFactory(catalog, DEV.settings.models, {})
    assert factory.code(ModelRef(model_id="claude-sonnet-4-6")) == "openai-gpt-4.1"
    assert (
        factory.code(ModelRef(model_id="anthropic.claude-haiku-4-5-20251001-v1:0")) == "openai-gpt-4.1-mini"
    )
    assert not DEV.models.models["claude-opus-4-8"].enabled
    assert factory.code(ModelRef(model_id="claude-opus-4-8")) == "openai-gpt-4.1"  # disabled, substituted
    assert factory.code(None) == "openai-gpt-4.1"  # the default model is substituted too
    with pytest.raises(ValueError, match="substitutes name unknown"):
        ModelCatalog.model_validate(
            {
                **json.loads(DEV.models.model_dump_json(by_alias=True)),
                "substitutes": {"claude-sonnet-4-6": "nope"},
            }
        )


def test_B02_model_strings_aliases_provider_ids_and_empty_models_resolve() -> None:
    factory = ModelFactory(DEV.models, DEV.settings.models, {})
    default = DEV.models.default_model
    assert factory.code(None) == default and factory.code(ModelRef(model_id=" ")) == default
    for code in DEV.models.models:
        if DEV.models.models[code].enabled:
            assert factory.code(ModelRef(model_id=code)) == code
            model_id = DEV.models.models[code].model_id
            if model_id not in DEV.models.models:  # a code wins over another entry's model ID
                assert factory.code(ModelRef(model_id=model_id)) == code
    for alias, code in DEV.models.aliases.items():
        if DEV.models.models[code].enabled:
            assert factory.code(ModelRef(model_id=alias)) == code
        else:  # an alias of a disabled model is refused like the model itself
            with pytest.raises(ZonicError) as caught:
                factory.code(ModelRef(model_id=alias))
            assert caught.value.code == "MODEL_NOT_ALLOWED"


def test_B02_the_platforms_azure_model_object_builds_an_azure_openai_model() -> None:
    resources = enabled(DEV, "gpt-5.2")
    factory = ModelFactory(resources.models, resources.settings.models, {})
    ref = ModelRef.model_validate({"modelId": "gpt-5.2", "provider": "azureopenai", "vendor": "openai"})
    entry = factory.entry(ref)
    assert entry.provider == "azure_openai"
    arguments = _provider_arguments(
        entry, lambda name: "https://example.invalid/" if name == entry.endpoint_env else None
    )
    assert arguments == {
        "azure_deployment": entry.deployment,
        "api_version": entry.api_version,
        "azure_endpoint": "https://example.invalid/",
    }


def test_B02_key_based_providers_get_their_key_and_endpoint_from_the_environment() -> None:
    from pydantic import SecretStr

    from zonic_runtime.config.env import EnvironmentModel
    from zonic_runtime.engine.agents.agent_factory import AgentServices

    env = EnvironmentModel(
        azure_openai_endpoint="https://example.invalid/",
        azure_openai_api_key=SecretStr("azure-key"),
        openai_api_key=SecretStr("openai-key"),
    )
    captured: dict[str, dict[str, Any]] = {}
    resources = enabled(DEV, "gpt-5.2")
    agents = AgentServices.from_resources(resources, environment=env)
    entry = agents.models.entry(ModelRef(model_id="gpt-5.2"))
    read = lambda field: (  # noqa: E731
        v.get_secret_value() if isinstance(v := getattr(env, field, None), SecretStr) else v
    )
    captured["azure"] = _provider_arguments(entry, read)
    openai = entry.model_copy(update={"provider": "openai", "endpoint_env": "azure_openai_endpoint"})
    captured["openai"] = _provider_arguments(openai, read)
    assert captured["azure"]["api_key"] == "azure-key" and captured["azure"]["azure_endpoint"]
    assert captured["openai"] == {"base_url": "https://example.invalid/", "api_key": "openai-key"}
    assert "api_key" not in _provider_arguments(DEV.models.models["claude-sonnet-4-6"], read)


def test_B02_openai_models_share_the_pooled_client_and_stream_their_usage() -> None:
    import httpx
    from pydantic import SecretStr

    from zonic_runtime.config.env import EnvironmentModel
    from zonic_runtime.engine.agents.agent_factory import AgentServices
    from zonic_runtime.integrations.http.pools import HttpPools

    pools = HttpPools()
    env = EnvironmentModel(openai_api_key=SecretStr("openai-key"))
    agents = AgentServices.from_resources(
        DEV, environment=env, http=lambda upstream: pools.get(upstream, httpx.AsyncClient)
    )
    first = agents.models.create(ModelRef(model_id="openai-gpt-4.1"))
    second = agents.models.create(ModelRef(model_id="openai-gpt-4.1-mini"))
    assert pools.upstreams == ["model:openai:"]
    assert getattr(first, "stream_usage", None) is True and getattr(second, "stream_usage", None) is True


@pytest.mark.parametrize("model", ["us.anthropic.claude-3-7-sonnet-20250219-v1:0", "gpt-5.2"])
async def test_N08_a_model_not_in_the_catalog_or_disabled_is_not_allowed(model: str) -> None:
    h, _ = agent_harness([Turn.say("never")], DEV)
    with pytest.raises(ZonicError) as caught:
        await h.run(chain(llm(model={"modelId": model})))
    assert caught.value.code == "MODEL_NOT_ALLOWED" and caught.value.details["model"] == model


async def test_N08_provider_throttling_is_retried_then_model_unavailable() -> None:
    throttled = ClientError(
        {"Error": {"Code": "ThrottlingException", "Message": "Rate exceeded"}}, "ConverseStream"
    )
    resources = fast_retry()
    attempts = resources.manifest.node_types["llm"].retry
    assert attempts is not None
    h, models = agent_harness([Turn.fail(throttled)] * attempts.max_attempts, resources)
    with pytest.raises(ZonicError) as caught:
        await h.run(chain(llm()))
    assert (
        caught.value.code == "MODEL_UNAVAILABLE" and "ThrottlingException" in caught.value.details["reason"]
    )
    assert len(models.model.requests) == attempts.max_attempts
    h, models = agent_harness([Turn.fail(throttled), Turn.say("recovered")], resources)
    final, *_ = await h.run(chain(llm()))
    assert final["nodes"]["n"]["output"]["text"] == "recovered"


# --- N-12, N-19, D-13 --------------------------------------------------------------------------


async def test_N12_variable_node_every_operation() -> None:
    updates = [
        {"path": "flow.name", "op": "set", "value": "Ada"},
        {"path": "flow.tags", "op": "append", "value": "c"},
        {"path": "flow.more", "op": "extend", "value": ["x", "y"]},
        {"path": "flow.profile", "op": "merge", "value": {"b": 2}},
        {"path": "flow.count", "op": "increment", "value": 2},
        {"path": "flow.gone", "op": "remove"},
        {"path": "flow.items", "op": "clear"},
    ]
    h, _ = agent_harness([], DEV)
    final, events, *_ = await h.run(
        chain(variable("v", updates)),
        flow(tags=["a", "b"], more=["w"], profile={"a": 1}, count=1, gone=True, items=[1, 2]),
    )
    assert final["flow"]["values"] == {
        "name": "Ada",
        "tags": ["a", "b", "c"],
        "more": ["w", "x", "y"],
        "profile": {"a": 1, "b": 2},
        "count": 3,
        "items": [],
    }
    changed = final["nodes"]["v"]["output"]
    assert changed["gone"] is None and changed["count"] == 3
    assert any(e["event"] == "state.updated" and "gone" in e["keys"] for e in events)


async def test_N19_output_mapping_builds_the_result_and_variable_updates_apply() -> None:
    mapping = [
        {"target": "answer", "value": "{{nodes.n.output.text}}", "type": "string"},
        {"target": "count", "value": "{{flow.count}}", "type": "number"},
        {"target": "raw", "value": "{{flow.profile}}"},
    ]
    out = output(mapping=mapping, variableUpdates=[{"path": "flow.done", "value": True}])
    h, _ = agent_harness([Turn.say("All set.")], DEV)
    final, *_ = await h.run(chain(llm(), out), flow(count="7", profile={"a": 1}))
    assert final["result"]["value"] == {"answer": "All set.", "count": 7, "raw": {"a": 1}}
    assert final["flow"]["values"]["done"] is True


async def test_D13_an_output_without_mapping_warns_and_the_fallback_result_applies() -> None:
    h, _ = agent_harness([Turn.say("From the model.")], DEV)
    run = await DriverHarness(h).run(chain(llm(), output()))
    assert run.outcome.status == "succeeded" and run.outcome.result == {"text": "From the model."}
    assert run.outcome.result_source == "last_llm_text"
    assert any(e["type"] == "warning" and e["data"]["code"] == "OUTPUT_MAPPING_MISSING" for e in run.events)


# --- S-09 --------------------------------------------------------------------------------------


async def test_S09_stream_tokens_off_tags_the_model_nostream_and_no_delta_is_published() -> None:
    script = [Turn.call("lookup", {"city": "Oslo"}), Turn.say("Quiet answer.")]
    h, models = agent_harness(script, DEV, tools={"n": [lookup]})
    run = await DriverHarness(h).run(chain(llm(streamResponse=False)))
    assert models.built[0][1]["tags"] == ["nostream"]
    assert run.text() == "" and run.outcome.state["nodes"]["n"]["output"]["text"] == "Quiet answer."
    assert [e["type"] for e in run.events if e["type"].startswith("tool.")] == [
        "tool.started",
        "tool.finished",
    ]


# --- router ------------------------------------------------------------------------------------


async def test_router_kind_takes_the_first_matching_branch_else_the_default() -> None:
    rule = {
        "id": "r",
        "type": "rule",
        "config": {
            "rules": [
                {"ruleId": "r_big", "conditions": [{"field": "{{flow.n}}", "op": "gt", "value": 10}]},
                {"ruleId": "r_else", "isDefault": True, "conditions": []},
            ]
        },
    }
    edges = [
        {"source": "start", "target": "r"},
        {"source": "r", "target": "big", "sourceHandle": "r_big"},
        {"source": "r", "target": "small", "sourceHandle": "r_else"},
    ]
    document = chain(rule, variable("big"), variable("small"), edges=edges)
    for value, taken in [(42, "big"), (3, "small")]:
        h, _ = agent_harness([], DEV)
        final, *_ = await h.run(document, flow(n=value))
        assert taken in final["nodes"] and ({"big", "small"} - {taken}).isdisjoint(final["nodes"])
        assert final["nodes"]["r"]["output"]["handle"] == ("r_big" if taken == "big" else "r_else")


async def test_S12_the_v2_fallback_matches_v3_for_the_agent_kind_with_tools_and_a_handoff() -> None:
    def script() -> list[Turn]:
        return [
            Turn.call("lookup", {"city": "Oslo"}, text="Checking."),
            Turn.call("handoff_to", {"target": "billing"}),
        ]

    runs = []
    for source in (None, V2EventSource()):
        h, _ = agent_harness(script(), DEV, tools={"ag": [lookup]})
        runs.append(await DriverHarness(h, source=source).run(handoff_document()))
    strip = [
        [
            {k: v for k, v in e.items() if k not in ("id", "ts")}
            | {"data": {k: v for k, v in e["data"].items() if k != "durationMs"}}
            for e in r.events
        ]
        for r in runs
    ]
    assert strip[0] == strip[1]
    assert {e.get("nodeId") for e in runs[0].events if e["type"] in ("token.delta", "tool.started")} == {"ag"}
