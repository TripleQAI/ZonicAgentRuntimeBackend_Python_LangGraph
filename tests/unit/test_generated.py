"""Generated files and the model base (P0-03): case M-03."""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel
from scripts import gen

from zonic_runtime.models.base import ZonicDocumentModel, ZonicModel
from zonic_runtime.models.gateway_meta import gateway_procedure, gateway_view, procedure_meta, view_meta
from zonic_runtime.models.markers import Column, SecretRef, TemplateField, column_map, marked_fields


def test_M03_generated_files_are_current() -> None:
    expected = gen.expected_files()
    stale = [
        str(p) for p, text in expected.items() if not p.exists() or p.read_text(encoding="utf-8") != text
    ]
    assert stale == [], "run: uv run python -m scripts.gen"


def test_M03_camel_case_on_the_wire_snake_case_in_python() -> None:
    class Sample(ZonicModel):
        run_id: str
        last_event_seq: int

    sample = Sample.model_validate({"runId": "r1", "lastEventSeq": 3})
    assert sample.run_id == "r1"
    assert sample.wire() == {"runId": "r1", "lastEventSeq": 3}
    assert Sample(run_id="r2", last_event_seq=1).run_id == "r2"


def test_M03_documents_keep_and_report_unknown_keys() -> None:
    class Doc(ZonicDocumentModel):
        name: str

    doc = Doc.model_validate({"name": "x", "originalBpc": "b", "createdBy": "u"})
    assert doc.unknown_keys() == ["createdBy", "originalBpc"]
    assert doc.model_dump(by_alias=True)["originalBpc"] == "b"


def test_M03_markers_and_gateway_metadata() -> None:
    @gateway_view(schema="runtime", name="v_run", owner_column="owner_sub")
    class Row(ZonicModel):
        run_id: Annotated[str, Column("run_id")]
        label: str

    @gateway_procedure(schema="runtime", name="sp_x", placeholders=2, idempotent=True)
    class Args(BaseModel):
        prompt: Annotated[str, TemplateField()]
        key: Annotated[str, SecretRef()]

    assert view_meta(Row).name == "v_run"
    assert view_meta(Row).owner_column == "owner_sub"
    assert procedure_meta(Args).placeholders == 2
    assert column_map(Row) == {"run_id": "run_id", "label": "label"}
    assert set(marked_fields(Args, TemplateField)) == {"prompt"}
    assert set(marked_fields(Args, SecretRef)) == {"key"}
