"""Content parts of the view kind (design 14.2; plan P3-08, cases N-18, B-11), after the Node.js
runtime's ``view-parts.ts``.

* ``sanitize_html``: the builder's ``htmlTemplate`` keeps only ``content.htmlAllowedTags`` and
  ``htmlAllowedAttributes``; scripts and styles go with their content, event handlers and
  ``javascript:`` URLs are removed, links keep only ``https`` and ``mailto``, images only inline
  ``data:image`` sources, and a ``style`` that loads or runs something is dropped.
* ``checked_chart_option``: an ECharts option is an object whose top-level keys are on
  ``content.chartOptionKeys`` and that holds no code (a function-valued formatter written as text
  is code, case N-18); otherwise ``CONTENT_INVALID``.
* ``view_data``: what a view's ``cacheDataKey`` names: a cache key, a dataset reference, or rows.
* ``grid_parts``: a ``grid`` over the referenced dataset, or a ``table`` of the inline rows (at
  most ``content.inlineRowLimit``).
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any

import nh3

from zonic_runtime.errors import ZonicError
from zonic_runtime.models.content import ChartPart, GridPart, HtmlPart, TableColumn, TablePart
from zonic_runtime.models.engine.records import DatasetRef
from zonic_runtime.models.resources.settings import ContentSettings

INVALID = "CONTENT_INVALID"
#: CSS that loads or runs something.
_ACTIVE_CSS = re.compile(r"url\s*\(|expression\s*\(|@import|behavior\s*:|-moz-binding", re.IGNORECASE)
#: A string that is code: ECharts formatters written as functions.
_CODE = re.compile(r"^\s*(async\s+)?function\b|=>|\bnew\s+Function\b|\beval\s*\(")
_LINK_SCHEMES = ("https:", "mailto:")
_IMAGE_DATA = "data:image/"
_GRID_FEATURES = (
    "filters",
    "rowGrouping",
    "pageSizeOptions",
    "theme",
    "height",
    "loadingMode",
    "grid_title_summary",
)
_REF_FIELDS = frozenset(DatasetRef.model_fields) | {
    f.alias for f in DatasetRef.model_fields.values() if f.alias
}
_IMG, _STYLE = "img", "style"
#: HTML elements removed with everything inside them.
_REMOVED_WITH_CONTENT = frozenset(
    {
        "script",  # principle: allow rule-1-node-type html-tag
        _STYLE,
        "iframe",
        "object",
        "embed",
        "noscript",
        "template",
    }
)


def sanitize_html(html: str, content: ContentSettings) -> str:
    attributes = {tag: set(names) for tag, names in content.html_allowed_attributes.items()}

    def keep(tag: str, attribute: str, value: str) -> str | None:
        if attribute == _STYLE:
            return None if _ACTIVE_CSS.search(value) else value
        if attribute in ("href", "src"):
            lowered = value.strip().lower()
            if tag == _IMG:
                return value if lowered.startswith(_IMAGE_DATA) else None
            return value if lowered.startswith(_LINK_SCHEMES) else None
        return value

    return nh3.clean(
        html,
        tags=set(content.html_allowed_tags),
        clean_content_tags=_REMOVED_WITH_CONTENT,
        attributes=attributes,
        attribute_filter=keep,
        url_schemes={"https", "mailto", "data"},
        strip_comments=True,
    )


def parse_json(text: str, node_id: str) -> Any:
    try:
        return json.loads(text)
    except ValueError as exc:
        raise ZonicError(INVALID, node_id=node_id, reason="the view data is not JSON") from exc


def checked_chart_option(option: Any, content: ContentSettings, node_id: str) -> dict[str, Any]:
    value = parse_json(option, node_id) if isinstance(option, str) else option
    if not isinstance(value, Mapping):
        raise ZonicError(INVALID, node_id=node_id, reason="the chart option is not an object")
    allowed = set(content.chart_option_keys)
    unknown = sorted(k for k in value if k not in allowed)
    if unknown:
        raise ZonicError(
            INVALID, node_id=node_id, reason=f"chart option keys not allowed: {', '.join(unknown)}"
        )

    def visit(item: Any, path: str) -> None:
        if callable(item):
            raise ZonicError(INVALID, node_id=node_id, reason=f"{path} is a function")
        if isinstance(item, str) and _CODE.search(item):
            raise ZonicError(INVALID, node_id=node_id, reason=f"{path} is code")
        if isinstance(item, list):
            for index, child in enumerate(item):
                visit(child, f"{path}[{index}]")
        elif isinstance(item, Mapping):
            for key, child in item.items():
                visit(child, f"{path}.{key}")

    visit(value, "option")
    return dict(value)


def view_data(value: Any) -> tuple[DatasetRef | None, list[dict[str, Any]] | None]:
    """``(reference, rows)``: a cache key (``cacheDataKey`` resolved from ``flow``, as the MCP server
    binds it), a dataset reference, or rows inline."""
    if isinstance(value, str) and value.strip():
        return DatasetRef(store="cache", key=value.strip()), None
    if (
        isinstance(value, Mapping)
        and isinstance(value.get("key"), str)
        and value.get("store") in ("cache", "database")
    ):
        return DatasetRef.model_validate({k: v for k, v in value.items() if k in _REF_FIELDS}), None
    if isinstance(value, list):
        return None, [dict(r) for r in value if isinstance(r, Mapping)]
    if isinstance(value, Mapping) and isinstance(value.get("rows"), list):
        return None, [dict(r) for r in value["rows"] if isinstance(r, Mapping)]
    return None, None


def grid_parts(config: Mapping[str, Any], content: ContentSettings) -> GridPart | TablePart:
    columns = [c for c in config.get("columnDefs") or [] if isinstance(c, Mapping)]
    title = config.get("title") if isinstance(config.get("title"), str) else None
    ref, rows = view_data(config.get("cacheDataKey"))
    if rows is not None:
        table_columns = [
            TableColumn(
                key=str(c.get("field") or c.get("key") or ""),
                label=c.get("headerName") if isinstance(c.get("headerName"), str) else None,
            )
            for c in columns
        ] or [TableColumn(key=k) for k in (rows[0] if rows else {})]
        return TablePart(
            title=title, columns=table_columns, rows=[dict(r) for r in rows[: content.inline_row_limit]]
        )
    page_size = config.get("pageSize")
    return GridPart(
        title=title,
        columns=[dict(c) for c in columns],
        dataset_ref=ref,
        page_size=page_size if isinstance(page_size, int) and page_size > 0 else None,
        features={k: config[k] for k in _GRID_FEATURES if k in config},
    )


def chart_part(option: dict[str, Any], ref: DatasetRef | None = None) -> ChartPart:
    return ChartPart(option=option, dataset_ref=ref)


def html_part(html: str, refs: list[DatasetRef] | None = None) -> HtmlPart:
    return HtmlPart(html=html, dataset_refs=list(refs or []))
