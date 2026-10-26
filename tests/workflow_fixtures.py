"""The vendored and synthetic workflow documents (plan Section 20), for every task that reads them."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from zonic_runtime.models.document import WorkflowDocument, WorkflowTemplate

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / "tests" / "fixtures" / "workflows"
BUILDER = WORKFLOWS / "builder"
SYNTHETIC = WORKFLOWS / "synthetic"
BUILDER_SCHEMA = BUILDER / "schema" / "workflow-document-v2.schema.json"


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def document_files() -> list[Path]:
    """Every workflow document file: vendored (except the schema) and synthetic."""
    vendored = [p for p in BUILDER.rglob("*.json") if p.parent.name != "schema" and p.name != "SOURCE.json"]
    return sorted(vendored) + sorted(SYNTHETIC.glob("*.json"))


def fixture_id(path: Path) -> str:
    return path.relative_to(WORKFLOWS).with_suffix("").as_posix()


def load_document(path: Path) -> WorkflowDocument:
    """A document file, unwrapping the builder's template files."""
    data = read_json(path)
    if path.parent.name == "templates":
        return WorkflowTemplate.model_validate(data).document
    return WorkflowDocument.model_validate(data)
