"""Resource files and settings (P0-04, P0-05): cases M-01, M-02."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from pydantic import BaseModel

from zonic_runtime.config.env import EnvironmentModel
from zonic_runtime.config.resources import deep_merge
from zonic_runtime.config.settings import load_resources, load_settings
from zonic_runtime.errors import StartupError
from zonic_runtime.models.resources import RESOURCE_FILES, RuntimeSettings

ROOT = Path(__file__).resolve().parents[2]
RESOURCES = ROOT / "resources"
ENVIRONMENTS = ["dev", "test", "prod"]


@pytest.mark.parametrize("env_name", ENVIRONMENTS)
def test_M01_every_resource_file_validates_in_every_overlay(env_name: str) -> None:
    env = EnvironmentModel(env=env_name, resources_dir=RESOURCES)
    resources = load_resources(env)
    assert resources.settings.observability.environment == env_name
    assert set(resources.manifest.node_types) >= {"start", "output", "note"}


def test_M01_every_resource_file_has_a_model() -> None:
    files = {p.name for p in RESOURCES.glob("*.json")}
    bases = {f for f in files if f.count(".") == 1}  # overlays look like name.<env>.json
    assert bases == set(RESOURCE_FILES) | {"settings.json"}


def test_M01_overlays_name_an_existing_base_file() -> None:
    for overlay in RESOURCES.glob("*.*.json"):
        stem, env_name, _ = overlay.name.rsplit(".", 2)
        assert (RESOURCES / f"{stem}.json").exists(), overlay.name
        assert env_name in ENVIRONMENTS, overlay.name


def _copy_resources(tmp_path: Path) -> Path:
    target = tmp_path / "resources"
    shutil.copytree(RESOURCES, target)
    return target


def _edit(path: Path, change: dict[str, object]) -> None:
    data = json.loads(path.read_text(encoding="utf-8"))
    path.write_text(json.dumps(deep_merge(data, change)), encoding="utf-8")


def test_M02_wrong_type_fails_with_file_and_path(tmp_path: Path) -> None:
    resources = _copy_resources(tmp_path)
    _edit(resources / "settings.json", {"engine": {"maxSteps": "many"}})
    with pytest.raises(StartupError) as info:
        load_settings(resources, "dev")
    assert info.value.code == "RESOURCE_INVALID"
    assert "settings.json" in info.value.details["file"]
    assert any(p["path"] == "engine.maxSteps" for p in info.value.details["problems"])


def test_M02_unknown_field_fails_with_path(tmp_path: Path) -> None:
    resources = _copy_resources(tmp_path)
    _edit(resources / "node-runtime.json", {"nodeTypes": {"llm": {"colour": "blue"}}})
    with pytest.raises(StartupError) as info:
        load_resources(EnvironmentModel(env="dev", resources_dir=resources))
    paths = [p["path"] for p in info.value.details["problems"]]
    assert "nodeTypes.llm.colour" in paths


def test_M02_missing_file_is_a_startup_error(tmp_path: Path) -> None:
    resources = _copy_resources(tmp_path)
    (resources / "models.json").unlink()
    with pytest.raises(StartupError) as info:
        load_resources(EnvironmentModel(env="dev", resources_dir=resources))
    assert info.value.code == "RESOURCE_MISSING"


def test_M02_malformed_json_reports_the_line(tmp_path: Path) -> None:
    resources = _copy_resources(tmp_path)
    (resources / "egress.json").write_text('{\n  "schemes": [\n', encoding="utf-8")
    with pytest.raises(StartupError) as info:
        load_resources(EnvironmentModel(env="dev", resources_dir=resources))
    assert info.value.code == "RESOURCE_INVALID"
    assert info.value.details["line"] >= 2


def _has_field_default(model: type[BaseModel], seen: set[type[BaseModel]]) -> list[str]:
    """Fields of the settings tree with a code default; tunables must live in settings.json."""
    found: list[str] = []
    if model in seen:
        return found
    seen.add(model)
    for name, info in model.model_fields.items():
        if not info.is_required():
            found.append(f"{model.__name__}.{name}")
        annotation = info.annotation
        if isinstance(annotation, type) and issubclass(annotation, BaseModel):
            found.extend(_has_field_default(annotation, seen))
    return found


def test_M02_settings_have_no_code_defaults() -> None:
    assert _has_field_default(RuntimeSettings, set()) == []


def test_M01_model_catalog_references_resolve() -> None:
    resources = load_resources(EnvironmentModel(env="dev", resources_dir=RESOURCES))
    assert resources.models.default_model in resources.models.models
    enabled = [code for code, entry in resources.models.models.items() if entry.enabled]
    assert enabled, "at least one model must be enabled"


def test_M01_retry_codes_exist_in_the_error_catalog() -> None:
    resources = load_resources(EnvironmentModel(env="dev", resources_dir=RESOURCES))
    codes = set(resources.errors.errors)
    entries = [
        resources.manifest.defaults.retry,
        *(e.retry for e in resources.manifest.node_types.values() if e.retry),
    ]
    for retry in entries:
        assert set(retry.on) <= codes, retry.on
    for code in (c for e in entries for c in e.on):
        assert resources.errors.errors[code].retryable, code


def test_the_aws_overlays_keep_the_dev_overlays_and_validate() -> None:
    """``deploy/aws/resources/`` replaces files of ``resources/`` in the AWS image: its
    ``settings.dev.json`` must keep every setting of the repository's own, and both validate."""
    import json
    import shutil
    import tempfile
    from pathlib import Path

    from zonic_runtime.config.env import EnvironmentModel
    from zonic_runtime.config.settings import load_resources

    root = Path(__file__).resolve().parents[2]
    aws = root / "deploy" / "aws" / "resources"
    ours = json.loads((root / "resources" / "settings.dev.json").read_text(encoding="utf-8"))
    theirs = json.loads((aws / "settings.dev.json").read_text(encoding="utf-8"))
    assert {k: v for k, v in theirs.items() if k != "datasets"} == ours
    assert theirs["datasets"] == {"store": "database"}  # no cache service on AWS
    with tempfile.TemporaryDirectory() as folder:
        image = Path(folder) / "resources"
        shutil.copytree(root / "resources", image)
        for file in aws.glob("*.json"):
            shutil.copy(file, image / file.name)
        found = load_resources(EnvironmentModel(env="dev", resources_dir=image))
    assert found.settings.datasets.store == "database"
    assert found.models.substitutes["claude-sonnet-4-6"] == "openai-gpt-4.1"
