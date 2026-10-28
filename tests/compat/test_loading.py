"""Every vendored document through the loader's one load path (P1-04: D-01, B-16, F-02).

The blueprint half of D-01 ("blueprint without errors") is P1-05's.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.workflow_fixtures import BUILDER, document_files, fixture_id, read_json
from zonic_runtime.config.env import EnvironmentModel
from zonic_runtime.config.settings import load_resources
from zonic_runtime.workflow.loader.loader import WorkflowLoader

RESOURCES = load_resources(EnvironmentModel(env="dev"))
LOADER = WorkflowLoader.from_resources(RESOURCES)
ZONIC = sorted((BUILDER / "zonic").glob("*.json"))
TEMPLATES = sorted((BUILDER / "templates").glob("*.json"))
PLATFORM = sorted((BUILDER / "platform").glob("*.json"))
FILES = document_files()


@pytest.mark.parametrize("path", ZONIC + TEMPLATES, ids=[fixture_id(p) for p in ZONIC + TEMPLATES])
def test_D01_zonic_workflows_and_templates_load(path: Path) -> None:
    loaded = LOADER.from_stored(read_json(path))
    assert loaded.document.nodes and len(loaded.revision) == 64
    assert not [f for f in loaded.findings if f.severity == "error"]


@pytest.mark.parametrize("path", FILES, ids=[fixture_id(p) for p in FILES])
def test_D01_every_fixture_loads_with_one_load_path(path: Path) -> None:
    loaded = LOADER.from_stored(read_json(path), "given-id")
    assert loaded.agentic_workflow_id == "given-id"
    assert loaded.version.split(".")[0] in RESOURCES.settings.document_versions


@pytest.mark.parametrize("path", PLATFORM + ZONIC, ids=[fixture_id(p) for p in PLATFORM + ZONIC])
def test_B16_fastapi_builder_documents_load_as_1_0(path: Path) -> None:
    # The platform fixtures are byte-identical to ZonicAgentBuilderServer_FastAPI/tests/fixtures.
    assert "schemaVersion" not in read_json(path)
    assert LOADER.from_stored(read_json(path)).version == "1.0"


@pytest.mark.parametrize("path", TEMPLATES, ids=lambda p: p.stem)
def test_F02_template_wrapper_loads_its_document_member(path: Path) -> None:
    wrapper = read_json(path)
    loaded = LOADER.from_stored(wrapper)
    assert loaded.agentic_workflow_id == wrapper["document"]["agenticWorkflowId"]
    assert loaded.agentic_workflow_id.startswith(tuple(RESOURCES.settings.catalog.excluded_id_prefixes))
    assert any(f.code == "MODEL_DEFAULTED" for f in loaded.findings)
