"""Every vendored document compiled (P1-09: G-01, F-15, B-01 compilation).

The snapshot of each compiled graph is committed under ``tests/fixtures/compiled/`` and
reviewed like code. Regenerate after an intended change with::

    ZONIC_UPDATE_SNAPSHOTS=1 uv run pytest tests/compat/test_compiled.py
"""

from __future__ import annotations

import json
import os
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from langgraph.graph import START

from tests.kinds.scripted_kind import ScriptedRuns
from tests.workflow_fixtures import BUILDER, document_files, fixture_id, read_json
from zonic_runtime.config.env import EnvironmentModel
from zonic_runtime.config.settings import load_resources
from zonic_runtime.models.blueprint import Blueprint
from zonic_runtime.workflow.blueprint.builder import BlueprintBuilder
from zonic_runtime.workflow.compiler.workflow_compiler import WorkflowCompiler
from zonic_runtime.workflow.loader.loader import WorkflowLoader

DEV = load_resources(EnvironmentModel(env="dev"))
RESOURCES = replace(
    DEV,
    settings=DEV.settings.model_copy(
        update={"features": DEV.settings.features.model_copy(update={"script_node": True})}
    ),
)
LOADER = WorkflowLoader.from_resources(RESOURCES)
BUILDER_ = BlueprintBuilder(RESOURCES)
COMPILER = WorkflowCompiler(ScriptedRuns().factory, RESOURCES.settings.engine)
SNAPSHOTS = Path(__file__).resolve().parents[1] / "fixtures" / "compiled"
INVALID = {BUILDER / "invalid" / "new-workflow.json"}
FILES = [p for p in document_files() if p not in INVALID and p.stem != "rule-expression"]


def plan(path: Path) -> Blueprint:
    return BUILDER_.build(LOADER.from_stored(read_json(path), fixture_id(path)))


def snapshot(blueprint: Blueprint, graph: Any) -> dict[str, Any]:
    nodes = {}
    for name, node in sorted(graph.builder.nodes.items()):
        spec = blueprint.by_graph_name(name)
        assert spec is not None
        retry = node.retry_policy
        nodes[name] = {
            "nodeId": spec.node_id,
            "kind": spec.kind,
            "defer": node.defer,
            "retryAttempts": retry.max_attempts if retry is not None else None,
            "timeoutSeconds": node.timeout.run_timeout if node.timeout is not None else None,
            "destinations": dict(sorted((node.ends or {}).items()))
            if isinstance(node.ends, dict)
            else list(node.ends or ()),
            "loopId": spec.loop_id,
        }
    return {"entry": blueprint.nodes[blueprint.entry].graph_name, "nodes": nodes}


@pytest.mark.parametrize("path", FILES, ids=[fixture_id(p) for p in FILES])
def test_G01_one_langgraph_node_per_compiled_builder_node(path: Path) -> None:
    blueprint = plan(path)
    graph = COMPILER.compile(blueprint, role="child")
    expected = {s.graph_name for s in blueprint.compiled_nodes()}
    assert set(graph.builder.nodes) == expected
    document = read_json(path)
    document = document.get("document", document)
    note_ids = {
        n["id"]
        for n in document["nodes"]
        if RESOURCES.manifest.node_types.get(n["type"], None) is not None
        and RESOURCES.manifest.node_types[n["type"]].kind == "ignore"
    }
    members = {s.node_id for s in blueprint.nodes.values() if s.container is not None}
    assert not ({blueprint.nodes[n].graph_name for n in members} | note_ids) & set(graph.builder.nodes)
    assert graph.builder.edges == {(START, blueprint.nodes[blueprint.entry].graph_name)}


@pytest.mark.parametrize("path", FILES, ids=[fixture_id(p) for p in FILES])
def test_F15_compiled_graphs_match_their_snapshot(path: Path) -> None:
    blueprint = plan(path)
    found = snapshot(blueprint, COMPILER.compile(blueprint, role="child"))
    target = SNAPSHOTS / f"{fixture_id(path).replace('/', '__')}.json"
    if os.environ.get("ZONIC_UPDATE_SNAPSHOTS") == "1":
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(found, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    assert target.is_file(), f"no snapshot for {fixture_id(path)}; run with ZONIC_UPDATE_SNAPSHOTS=1"
    assert found == json.loads(target.read_text(encoding="utf-8"))


def test_F15_every_snapshot_belongs_to_a_fixture() -> None:
    expected = {f"{fixture_id(p).replace('/', '__')}.json" for p in FILES}
    assert {p.name for p in SNAPSHOTS.glob("*.json")} == expected
