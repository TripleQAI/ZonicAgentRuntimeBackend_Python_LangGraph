"""The view kind and content parts (P3-08: N-18, B-11, M-06)."""

from __future__ import annotations

import json
from typing import Any

import pytest
from pydantic import TypeAdapter

from tests.api.app import auth, make_app
from tests.fakes.scripted_chat_model import Turn
from tests.kinds.harness import DEFAULT_INPUT, Harness
from zonic_runtime.config.env import EnvironmentModel
from zonic_runtime.config.settings import load_resources
from zonic_runtime.engine.kinds.core import core_kinds
from zonic_runtime.errors import ZonicError
from zonic_runtime.io.view_parts import sanitize_html
from zonic_runtime.models.content import (
    CONTENT_PART,
    CardAction,
    CardField,
    CardPart,
    ChartPart,
    CitationPart,
    ErrorPart,
    FilePart,
    FormPart,
    GridPart,
    HtmlPart,
    JsonPart,
    MarkdownPart,
    TableColumn,
    TablePart,
)
from zonic_runtime.models.engine.records import DatasetRef
from zonic_runtime.models.events import RunEvent

DEV = load_resources(EnvironmentModel(env="dev"))
CONTENT = DEV.settings.content
EVENT = TypeAdapter(RunEvent)


def view(**config: Any) -> dict[str, Any]:
    return {
        "schemaVersion": "2.0",
        "nodes": [
            {"id": "start", "type": "start", "config": {}},
            {"id": "v", "type": "view", "name": "v", "config": config},
        ],
        "edges": [{"source": "start", "target": "v"}],
    }


def flow(**values: Any) -> dict[str, Any]:
    return {**DEFAULT_INPUT, "flow": {"values": values, "provenance": {}, "conflicts": []}}


async def run(config: dict[str, Any], **values: Any) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    final, events, *_ = await Harness(DEV, core_kinds()).run(view(**config), flow(**values))
    return final["nodes"]["v"], [e for e in events if e.get("event") == "content.part"]


GRID = {
    "viewType": "grid",
    "title": "Bills",
    "cacheDataKey": "{{flow.billsKey}}",
    "pageSize": 25,
    "theme": "ag-theme-alpine",
    "filters": [{"name": "site", "type": "text"}],
    "columnDefs": [{"field": "Site", "headerName": "Site"}, {"field": "Usage"}],
}


# --- N-18 --------------------------------------------------------------------------------------


async def test_N18_a_grid_view_is_a_grid_part_over_the_cached_dataset() -> None:
    record, events = await run(
        {"viewMode": "template_config", "templateConfig": GRID}, billsKey="bills:v1:abc"
    )
    [part] = record["parts"]
    assert part == {
        "type": "grid",
        "title": "Bills",
        "columns": GRID["columnDefs"],
        "datasetRef": {"store": "cache", "key": "bills:v1:abc", "kind": "rows", "columns": [], "preview": []},
        "pageSize": 25,
        "features": {"filters": GRID["filters"], "theme": "ag-theme-alpine"},
    }
    assert record["output"] == {"viewType": "grid", "datasetRef": part["datasetRef"]}  # fields spread
    assert [e["part"] for e in events] == [part] and events[0]["type"] == "grid"


async def test_N18_inline_rows_are_a_table_part_capped_at_the_inline_row_limit() -> None:
    rows = [{"Site": f"S{i}", "Usage": i} for i in range(CONTENT.inline_row_limit + 5)]
    record, _ = await run({"templateConfig": {**GRID, "cacheDataKey": rows}})
    [part] = record["parts"]
    assert part["type"] == "table" and len(part["rows"]) == CONTENT.inline_row_limit
    assert part["columns"] == [{"key": "Site", "label": "Site"}, {"key": "Usage"}]


async def test_N18_a_grid_without_data_warns() -> None:
    record, _ = await run({"templateConfig": {**GRID, "cacheDataKey": ""}})
    assert record["parts"][0]["type"] == "grid" and "datasetRef" not in record["parts"][0]


async def test_N18_a_chart_view_is_a_checked_chart_part_and_its_html() -> None:
    option = {
        "title": {"text": "Usage"},
        "xAxis": {"type": "category", "data": ["Jan"]},
        "series": [{"type": "bar", "data": [3]}],
    }
    config = {"viewType": "chart", "chart_data": "{{flow.chart}}", "html_data": "<b>{{flow.note}}</b>"}
    record, events = await run(
        {"viewMode": "template_config", "templateConfig": config}, chart=option, note="ok"
    )
    chart, html = record["parts"]
    assert chart == {"type": "chart", "library": "echarts", "option": option}
    assert html["type"] == "html" and html["html"] == "<b>ok</b>"
    assert record["output"]["html"] == "<b>ok</b>" and [e["type"] for e in events] == ["chart", "html"]


@pytest.mark.parametrize(
    "option",
    [
        {"tooltip": {"formatter": "function (p) { return p.value + ' kWh'; }"}},
        {"series": [{"label": {"formatter": "(p) => p.name"}}]},
        {"title": {"text": "x"}, "graphic": {"type": "text"}},  # a key off the allow list
        "not json at all",
        ["an", "array"],
    ],
)
async def test_N18_a_function_valued_or_unknown_chart_option_is_content_invalid(option: Any) -> None:
    with pytest.raises(ZonicError) as caught:
        await run({"templateConfig": {"viewType": "chart", "chart_data": "{{flow.chart}}"}}, chart=option)
    assert caught.value.code == "CONTENT_INVALID"


async def test_N18_an_unknown_view_type_is_content_invalid() -> None:
    with pytest.raises(ZonicError, match="CONTENT_INVALID"):
        await run({"templateConfig": {"viewType": "carousel"}})


# --- B-11 --------------------------------------------------------------------------------------


async def test_B11_an_html_template_view_escapes_values_and_is_sanitized() -> None:
    template = (
        '<div class="card" onclick="steal()"><h2>{{flow.title}}</h2>'
        '<a href="javascript:alert(1)">bad</a><a href="https://example.com/r">ok</a>'
        '<img src="https://tracker.example/p.gif"><img src="data:image/png;base64,AAAA" alt="x">'
        '<p style="background:url(https://x/y)">styled</p><p style="color:red">red</p>'
        "<script>alert(1)</script><style>body{}</style><iframe src='https://x'></iframe></div>"
    )
    record, events = await run({"htmlTemplate": template}, title="<script>alert('x')</script>")
    html = record["output"]["html"]
    assert "&lt;script&gt;alert(" in html  # the value is text, not markup
    for gone in ("onclick", "javascript:", "tracker.example", "url(", "<script", "<style", "<iframe"):
        assert gone not in html, gone
    assert 'href="https://example.com/r"' in html and 'src="data:image/png;base64,AAAA"' in html
    assert 'style="color:red"' in html and 'class="card"' in html
    assert record["parts"] == [{"type": "html", "html": html, "datasetRefs": []}] and len(events) == 1


def test_B11_the_sanitizer_keeps_only_allowed_tags_and_attributes() -> None:
    cleaned = sanitize_html(
        '<marquee>x</marquee><table data-x="1" colspan="2"><tr><td>1</td></tr></table>', CONTENT
    )
    assert "<marquee" not in cleaned and "data-x" not in cleaned and 'colspan="2"' in cleaned


async def test_B11_view_parts_are_streamed_and_kept_in_the_answer_message() -> None:
    document = {
        "schemaVersion": "2.0",
        "nodes": [
            {"id": "start", "type": "start", "config": {}},
            {
                "id": "llm",
                "type": "llm",
                "name": "llm",
                "config": {"messages": [{"role": "user", "content": "{{system.userQuery}}"}]},
            },
            {
                "id": "v",
                "type": "view",
                "name": "v",
                "config": {"htmlTemplate": "<p>{{nodes.llm.output.text}}</p>"},
            },
            {
                "id": "out",
                "type": "output",
                "config": {"outputMapping": [{"target": "answer", "value": "{{nodes.llm.output.text}}"}]},
            },
        ],
        "edges": [
            {"source": "start", "target": "llm"},
            {"source": "llm", "target": "v"},
            {"source": "v", "target": "out"},
        ],
    }
    app = make_app([Turn.say("Two sites <b>over</b> budget.")], workflows={"wf-chat": document})
    async with app.client() as client:
        stream = await client.post(
            "/v1/runs",
            json={"agenticWorkflowId": "wf-chat", "input": {"text": "status?"}, "mode": "stream"},
            headers=auth(),
        )
        events = [json.loads(line[6:]) for line in stream.text.splitlines() if line.startswith("data: ")]
        session = stream.headers["x-session-id"]
        messages = (await client.get(f"/v1/sessions/{session}/messages", headers=auth())).json()["items"]
    [part_event] = [e for e in events if e["type"] == "content.part"]
    EVENT.validate_python(part_event)  # the event's part is a typed ContentPart
    assert part_event["data"]["part"]["html"] == "<p>Two sites &lt;b&gt;over&lt;/b&gt; budget.</p>"
    answer = messages[0]
    assert answer["role"] == "assistant"
    assert [p["type"] for p in answer["contentParts"]] == ["markdown", "html"]
    assert answer["contentParts"][1] == part_event["data"]["part"]


# --- M-06 --------------------------------------------------------------------------------------

REF = DatasetRef(store="cache", key="k1", dataset_id="d-1", row_count=3, columns=["a"])
PARTS = [
    MarkdownPart(text="**hi**"),
    TablePart(title="t", columns=[TableColumn(key="a", label="A")], rows=[{"a": 1}], dataset_ref=REF),
    GridPart(columns=[{"field": "a"}], dataset_ref=REF, page_size=10, features={"theme": "x"}),
    ChartPart(option={"series": []}, dataset_ref=REF),
    CardPart(
        title="c",
        body="b",
        fields=[CardField(label="l", value=1)],
        actions=[CardAction(label="go", action="open")],
    ),
    JsonPart(value={"x": [1, 2]}),
    FilePart(name="r.csv", media_type="text/csv", download_ref="dl-1"),
    HtmlPart(html="<p>x</p>", dataset_refs=[REF]),
    CitationPart(title="src", url="https://example.com", snippet="s"),
    ErrorPart(code="TIMEOUT", message="slow"),
    FormPart(interrupt_id="i-1", schema={"type": "object"}, ui_schema={"a": {}}),
]


@pytest.mark.parametrize("part", PARTS, ids=[p.type for p in PARTS])
def test_M06_every_content_part_round_trips_as_its_own_class_with_identical_json(part: Any) -> None:
    wire = part.wire()
    again = CONTENT_PART.validate_python(json.loads(json.dumps(wire)))
    assert type(again) is type(part) and again.wire() == wire


def test_M06_an_unknown_part_type_is_refused() -> None:
    with pytest.raises(ValueError):
        CONTENT_PART.validate_python({"type": "carousel", "slides": []})
