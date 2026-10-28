"""Node configuration, engine record and blueprint models (P1-02): cases M-05, M-08, and the
configuration half of M-04."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from tests.workflow_fixtures import BUILDER_SCHEMA, document_files, fixture_id, load_document, read_json
from zonic_runtime.config.env import EnvironmentModel
from zonic_runtime.config.settings import load_resources
from zonic_runtime.models.blueprint import (
    Blueprint,
    BlueprintWarning,
    EdgeIndex,
    JoinSpec,
    NodeSpec,
    RunLimits,
)
from zonic_runtime.models.engine import (
    DatasetRef,
    FlowPatch,
    InterruptRequest,
    NodeOutcome,
    NodeRecord,
    StatePatch,
    StateUpdate,
    ThreadMessage,
    TimelineEntry,
    Usage,
)
from zonic_runtime.models.markers import SecretRef, TemplateField, marked_fields, marked_values
from zonic_runtime.models.nodes import (
    AgentNodeConfig,
    ExternalAgentNodeConfig,
    LlmNodeConfig,
    NodeConfigBase,
    RouterNodeConfig,
    config_model_for,
    config_models,
    is_secret_reference,
    kind_of,
)

MANIFEST = load_resources(EnvironmentModel(env="dev")).manifest
KIND = {name: entry.kind for name, entry in MANIFEST.node_types.items()}
FILES = document_files()


def _config(node_type: str, config: dict[str, Any]) -> NodeConfigBase:
    return config_model_for(KIND[node_type]).model_validate(config)


# --- M-05: every node of every fixture converts --------------------------------------


def test_M05_every_manifest_kind_has_one_configuration_model() -> None:
    assert set(config_models()) == set(KIND.values())
    assert len(config_models()) == 17
    assert kind_of(config_model_for("multi_agent")) == "multi_agent"


@pytest.mark.parametrize("path", FILES, ids=[fixture_id(p) for p in FILES])
def test_M05_every_node_converts_to_its_configuration_model(path: Path) -> None:
    for node in load_document(path).nodes:
        config = _config(node.type, node.config)
        assert config.unknown_keys() == [], f"{node.id}: {config.unknown_keys()}"


def test_M05_configuration_round_trips_by_alias() -> None:
    document = load_document(next(p for p in FILES if p.name == "facility_operations_swarm.json"))
    for node in document.nodes:
        config = _config(node.type, node.config)
        assert config.model_dump(mode="json", by_alias=True, exclude_unset=True) == node.config


def test_M05_rule_branches_are_typed() -> None:
    document = load_document(
        next(p for p in FILES if p.name == "fsh-facility-agent.json" and p.parent.name == "zonic")
    )
    rule = next(n for n in document.nodes if KIND[n.type] == "router")
    config = _config(rule.type, rule.config)
    assert isinstance(config, RouterNodeConfig)
    assert [b.rule_id for b in config.rules] == ["r_default", "r_else"]
    assert config.rules[0].conditions[0].op == "eq" and config.rules[1].is_default


def test_M05_engine_records_round_trip() -> None:
    usage = Usage(input_tokens=10, output_tokens=5, total_tokens=15, model_calls=1)
    message = ThreadMessage(
        role="assistant",
        kind="answer",
        node_id="agent_1",
        visit=1,
        text_preview="hi",
        content_parts=[{"type": "text", "text": "hi"}],
    )
    dataset = DatasetRef(
        key="db_1:1",
        dataset_id="d1",
        store="cache",
        kind="rows",
        node_id="db_1",
        visit=1,
        row_count=3,
        columns=["a"],
        preview=[{"a": 1}],
    )
    patch = StatePatch(
        flow=FlowPatch(step=3, writer="variable_1", set={"topic": "billing"}),
        node=NodeRecord(
            node_id="agent_1",
            node_type="agent",
            status="succeeded",
            visit=1,
            output={"text": "hi"},
            usage=usage,
        ),
        messages=[message],
        timeline=[
            TimelineEntry(
                node_id="agent_1",
                node_type="agent",
                visit=1,
                status="succeeded",
                started_at=datetime(2026, 10, 26, tzinfo=UTC),
                duration_ms=12.5,
                handles=["default"],
            )
        ],
        datasets=[dataset],
        usage=usage,
    )
    assert StatePatch.model_validate(patch.wire()) == patch
    outcome = NodeOutcome(output={"text": "hi"}, activate=["*"], messages=[message])
    assert outcome.activates_all and NodeOutcome.model_validate(outcome.wire()) == outcome
    request = InterruptRequest(
        kind="approval", node_id="approval_1", prompt={"message": "ok?"}, answer_schema={"type": "object"}
    )
    assert InterruptRequest.model_validate(request.wire()) == request
    update = StateUpdate.model_validate(
        {
            "path": "flow.count",
            "op": "increment",
            "value": 1,
            "when": {
                "conditionLogic": "AND",
                "conditions": [{"field": "{{flow.on}}", "op": "eq", "value": True}],
            },
        }
    )
    assert update.when is not None and update.when.conditions[0].field == "{{flow.on}}"


def test_M05_engine_records_reject_unknown_fields_and_bad_values() -> None:
    with pytest.raises(ValidationError):
        NodeRecord.model_validate(
            {"nodeId": "a", "nodeType": "agent", "status": "succeeded", "visit": 1, "oops": 1}
        )
    with pytest.raises(ValidationError):
        StateUpdate.model_validate({"path": "flow.x", "op": "multiply"})
    with pytest.raises(ValidationError):
        TimelineEntry.model_validate(
            {
                "nodeId": "a",
                "nodeType": "agent",
                "visit": 0,
                "status": "succeeded",
                "startedAt": "2026-10-26T00:00:00Z",
                "durationMs": 1,
            }
        )
    assert (Usage(input_tokens=1, cost=0.5) + Usage(input_tokens=2)).wire() == {
        "inputTokens": 3,
        "outputTokens": 0,
        "totalTokens": 0,
        "modelCalls": 0,
        "cost": 0.5,
    }


def _blueprint() -> Blueprint:
    agent = AgentNodeConfig.model_validate(
        {"model": "claude-haiku-4-5", "systemPrompt": "Hi {{system.userQuery}}"}
    )
    note = config_model_for("ignore").model_validate({"text": "about"})
    return Blueprint(
        workflow_id="wf",
        revision="r1",
        document_version="2.0",
        graph_id="wf@r1",
        entry="start_1",
        limits=RunLimits(max_visits_per_node=10, max_steps=200),
        nodes={
            "start_1": NodeSpec(
                node_id="start_1",
                node_type="start",
                kind="entry",
                name="Start_1",
                graph_name="start_1",
                config=config_model_for("entry")(),
            ),
            "agent_1": NodeSpec(
                node_id="agent_1",
                node_type="agent",
                kind="agent",
                name="Agent_1",
                graph_name="agent_1",
                config=agent,
                join=JoinSpec(barrier=True, sources=["a", "b"]),
            ),
            "note_1": NodeSpec(
                node_id="note_1",
                node_type="note",
                kind="ignore",
                name="About",
                graph_name="note_1",
                config=note,
            ),
            "member_1": NodeSpec(
                node_id="member_1",
                node_type="agent",
                kind="agent",
                name="Member",
                graph_name="member_1",
                config=agent,
                container="swarm_1",
            ),
        },
        edge_index=EdgeIndex(edges={"start_1": {"default": ["agent_1"]}}),
        name_index={"Agent_1": "agent_1"},
        warnings=[
            BlueprintWarning(code="NODE_UNREACHABLE", node_id="x", message="x is unreachable"),
            BlueprintWarning(code="SUBFLOW_CYCLE", severity="error", message="cycle"),
        ],
    )


def test_M05_blueprint_models() -> None:
    blueprint = _blueprint()
    assert [s.node_id for s in blueprint.compiled_nodes()] == ["start_1", "agent_1"]
    assert blueprint.resolve("Agent_1") is blueprint.resolve("agent_1") is not None
    assert blueprint.edge_index.targets("start_1", "default") == ["agent_1"]
    assert blueprint.edge_index.targets("start_1", "missing") == []
    assert [w.code for w in blueprint.errors] == ["SUBFLOW_CYCLE"]
    assert blueprint.nodes["agent_1"].trace_metadata()["zonic_node_id"] == "agent_1"
    restored = Blueprint.model_validate(blueprint.model_dump(mode="json", by_alias=True))
    assert restored.nodes["agent_1"].join.barrier
    with pytest.raises(ValidationError):
        blueprint.entry = "other"  # type: ignore[misc]


# --- M-08: TemplateField and SecretRef --------------------------------------------------


def test_M08_only_marked_fields_are_template_fields() -> None:
    config = AgentNodeConfig.model_validate(
        {
            "model": "{{flow.model}}",
            "strategy": "{{flow.strategy}}",
            "systemPrompt": "Answer {{system.userQuery}}",
            "userQueryMapping": "{{flow.question}}",
        }
    )
    values = marked_values(config, TemplateField)
    assert values == {
        "system_prompt": "Answer {{system.userQuery}}",
        "user_query_mapping": "{{flow.question}}",
    }
    assert "model" not in marked_fields(AgentNodeConfig, TemplateField)
    assert {"messages", "prompt_template", "user_query_mapping"} <= set(
        marked_fields(LlmNodeConfig, TemplateField)
    )


def test_M08_every_configuration_model_marks_its_updates_as_templates() -> None:
    for model in config_models().values():
        assert {"variable_updates", "state_updates"} <= set(marked_fields(model, TemplateField))


@pytest.mark.parametrize("value", ["sk-live-1234567890", "Bearer abc", "password123", "{{flow.token}}"])
def test_M08_literal_secret_is_rejected(value: str) -> None:
    assert not is_secret_reference(value)
    with pytest.raises(ValidationError, match="literal secret"):
        ExternalAgentNodeConfig.model_validate({"endpoint": "https://agent.example", "apiKey": value})


@pytest.mark.parametrize(
    "value", ["secret:external/agent-token", "{{secrets.externalAgentToken}}", "{{ secrets.x }}", "", None]
)
def test_M08_secret_reference_is_accepted(value: str | None) -> None:
    config = ExternalAgentNodeConfig.model_validate({"apiKey": value})
    assert marked_fields(ExternalAgentNodeConfig, SecretRef).keys() == {"api_key"}
    assert config.api_key == value


def test_M08_fixtures_hold_no_literal_secret() -> None:
    for path in FILES:
        for node in load_document(path).nodes:
            _config(node.type, node.config)  # a literal secret would raise


# --- M-04: configuration models against the builder schema ----------------------------


def test_M04_configuration_models_agree_with_the_builder_schema() -> None:
    schema = read_json(BUILDER_SCHEMA)["$defs"]
    problems = []
    for node_type, kind in KIND.items():
        builder = schema.get(f"config.{node_type}", {}).get("properties", {})
        model = config_model_for(kind).model_json_schema(by_alias=True)["properties"]
        problems += [
            f"{node_type}.{key}: in the builder schema, not in {config_model_for(kind).__name__}"
            for key in builder
            if key not in model
        ]
    assert problems == []
