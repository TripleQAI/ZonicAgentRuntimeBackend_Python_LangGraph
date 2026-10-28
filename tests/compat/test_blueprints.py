"""Every vendored document through loader and blueprint (P1-05: B-01, F-01, F-03).

``script`` nodes are behind the ``scriptNode`` feature flag (off by default); these tests turn
it on, so every builder document plans. Compilation, the last step of B-01, is P1-09's.
"""

from __future__ import annotations

import shutil
from dataclasses import replace
from pathlib import Path

import pytest
from scripts import sync_builder

from tests.workflow_fixtures import BUILDER, document_files, fixture_id, read_json
from zonic_runtime.config.env import EnvironmentModel
from zonic_runtime.config.settings import load_resources
from zonic_runtime.errors import ZonicError
from zonic_runtime.models.blueprint import Blueprint
from zonic_runtime.workflow.blueprint.builder import BlueprintBuilder
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
INVALID = BUILDER / "invalid" / "new-workflow.json"
VALID = [p for p in document_files() if p != INVALID and p.stem != "rule-expression"]


def blueprint(path: Path) -> Blueprint:
    return BUILDER_.build(LOADER.from_stored(read_json(path), fixture_id(path)))


@pytest.mark.parametrize("path", VALID, ids=[fixture_id(p) for p in VALID])
def test_B01_every_document_plans(path: Path) -> None:
    plan = blueprint(path)
    assert not plan.errors
    assert plan.nodes[plan.entry].compiled
    compiled = {s.node_id for s in plan.compiled_nodes()}
    for source, by_handle in plan.edge_index.edges.items():
        assert source in compiled
        assert all(t in compiled for targets in by_handle.values() for t in targets)
    assert all(s.handles for s in plan.compiled_nodes() if not s.terminal)


def test_B01_platform_loop_is_one_component_with_several_entries() -> None:
    plan = blueprint(BUILDER / "platform" / "intelligent-buying-orchestrator.json")
    assert len(plan.components) == 1
    loop = plan.components[0]
    assert len(loop.nodes) >= 10 and len(loop.entries) >= 2 and loop.has_exit


@pytest.mark.parametrize("name", sorted(p.name for p in (BUILDER / "zonic").glob("*.json")))
def test_F01_explicit_1_0_copy_gives_the_same_blueprint(name: str) -> None:
    implicit, explicit = blueprint(BUILDER / "zonic" / name), blueprint(BUILDER / "zonic-versioned" / name)
    assert explicit.document_version == implicit.document_version == "1.0"
    assert explicit.edge_index == implicit.edge_index
    assert explicit.components == implicit.components
    assert {k: (s.kind, s.handles, s.join) for k, s in explicit.nodes.items()} == {
        k: (s.kind, s.handles, s.join) for k, s in implicit.nodes.items()
    }


def test_F03_new_workflow_fails_with_sw_1() -> None:
    with pytest.raises(ZonicError) as caught:
        blueprint(INVALID)
    assert caught.value.code == "PLAN_INVALID"
    assert "SW-1" in {p["details"].get("rule_id") for p in caught.value.details["problems"]}


def test_F03_a_changed_fixture_fails_the_drift_check(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    copy = tmp_path / "builder"
    shutil.copytree(sync_builder.TARGET, copy)
    monkeypatch.setattr(sync_builder, "TARGET", copy)
    monkeypatch.setattr(sync_builder, "MANIFEST", copy / "SOURCE.json")
    no_repos = tmp_path / "no-repos"
    assert sync_builder.check(no_repos) == 0
    changed = copy / "zonic" / "all-agents.json"
    original = changed.read_bytes()
    changed.write_bytes(original + b" ")
    assert sync_builder.check(no_repos) == 1
    changed.write_bytes(original)
    registry = tmp_path / "builder-registry.json"
    registry.write_bytes(sync_builder.REGISTRY.read_bytes().replace(b'"SW-1"', b'"SW-01"'))
    monkeypatch.setattr(sync_builder, "REGISTRY", registry)
    monkeypatch.setattr(sync_builder, "ROOT", tmp_path)
    assert sync_builder.check(no_repos) == 1


def test_F03_the_registry_names_every_manifest_node_type() -> None:
    assert set(RESOURCES.builder.node_types) == set(RESOURCES.manifest.node_types)
    assert {r.id for rules in RESOURCES.builder.validation_rules.values() for r in rules} == {
        f"SW-{i}" for i in range(1, 13)
    }
