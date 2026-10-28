"""The ``http``, ``sandbox`` and ``view`` kinds (design 8.4; plan P3-03, P3-07, P3-08; cases N-14,
X-03, N-17, N-18, B-11).

``http``: one call through the egress port with the node's method, list headers (``key`` and
``value``), body and timeout. The answer is the output (``status``, ``headers``, ``body`` as
text, ``json`` when the body is JSON) and follows ``default`` whatever its status; a refused
address is ``EGRESS_DENIED``, a 5xx answer or no answer ``UPSTREAM_5XX``, a slow one
``TIMEOUT``. The manifest's retry policy retries those for the methods it lists (GET and HEAD),
never for a POST, which may have had its effect.

The ``url`` port binding encodes the values a template puts into the URL's path or query
(``encode: uriComponent``, ``io/encoders.py``; X-03).

The node's ``timeoutSeconds`` is also its node timeout (the manifest's ``timeoutSecondsFrom``),
which is not retried; the request gets ``_REQUEST_SHARE`` of it, so a slow answer is the
egress's ``TIMEOUT``, retried for GET, and the node timeout stays the outer cap.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from zonic_runtime.engine.kinds.flow_kinds import _Kind
from zonic_runtime.engine.node_template import NodeContext
from zonic_runtime.errors import ZonicError
from zonic_runtime.io.rows import held_within, preview_text
from zonic_runtime.io.view_parts import (
    chart_part,
    checked_chart_option,
    grid_parts,
    html_part,
    parse_json,
    sanitize_html,
    view_data,
)
from zonic_runtime.models.engine import NodeOutcome
from zonic_runtime.models.io.node_io import NodeInput
from zonic_runtime.ports.egress import EgressRequest, EgressResponse
from zonic_runtime.ports.sandbox import SandboxRequest
from zonic_runtime.workflow.templates.updates import convert_mapped

_DEFAULT_METHOD = "GET"
_REQUEST_SHARE = 0.9
_URL, _HEADERS, _BODY = "url", "headers", "body"
_JSON_TYPES = ("application/json", "+json")
_INPUTS = "inputs"
_DEFAULT_LANGUAGE = "javascript"


def parsed_body(response: EgressResponse, text: str) -> Any:
    kind = response.headers.get("content-type", "").split(";")[0].strip().lower()
    if not (kind.endswith(_JSON_TYPES) or text.lstrip()[:1] in ("{", "[")):
        return None
    try:
        return json.loads(text)
    except ValueError:
        return None


class HttpKind(_Kind):
    async def execute(self, ctx: NodeContext, node_input: NodeInput) -> NodeOutcome:
        egress = ctx.services.ports.egress
        if egress is None:
            raise ZonicError("RESOURCE_MISSING", resource="egress", node_id=self.spec.node_id)
        config = self.spec.config
        headers = tuple(
            (str(h["key"]), "" if h.get("value") is None else str(h["value"]))
            for h in node_input.get(_HEADERS) or []
            if isinstance(h, Mapping) and h.get("key")
        )
        request = EgressRequest(
            method=str(getattr(config, "method", None) or _DEFAULT_METHOD).upper(),
            url=str(node_input.get(_URL) or ""),
            headers=headers,
            body=node_input.get(_BODY),
            timeout_seconds=(self.spec.timeout_seconds or 0) * _REQUEST_SHARE or None,
        )
        if not request.url:
            raise ZonicError("INPUT_INVALID", node_id=self.spec.node_id, field="config.url", reason="empty")
        response = await egress.send(request)
        text = response.text()
        json_body = parsed_body(response, text)
        io = ctx.services.resources.settings.io
        complete = len(text.encode("utf-8")) <= io.inline_bytes
        output: dict[str, Any] = {
            "status": response.status,
            "headers": response.headers,
            "body": preview_text(text, io.inline_bytes),
            "json": json_body if complete else None,
        }
        # The whole response is a dataset (design 8.2); the output keeps a preview of the body.
        datasets = ctx.run.ports.datasets
        if datasets is None:
            return NodeOutcome(output=output, activate=self.default(ctx))
        ref = await datasets.port.keep(
            datasets.owner(self.spec.node_id, ctx.visit),
            "http_response",
            {"status": response.status, "headers": response.headers, "body": text},
            datasets.port.store(),
            response.headers.get("content-type") or "application/octet-stream",
        )
        if not complete and held_within(text, io.max_resolved_bytes):
            datasets.pending.held[self.spec.node_id] = {
                "body": text,
                **({"json": json_body} if json_body is not None else {}),
            }
        output["datasetRef"] = ref.wire()
        return NodeOutcome(output=output, activate=self.default(ctx), datasets=[ref])


def mapping_record(value: Any) -> dict[str, Any]:
    """A resolved mapping-list port (``[{key, value, type?}]``) as a record, each value converted
    to its declared type; a record passes through."""
    if isinstance(value, Mapping):
        return dict(value)
    found: dict[str, Any] = {}
    for entry in value if isinstance(value, list) else []:
        if isinstance(entry, Mapping) and isinstance(entry.get("key"), str) and entry["key"]:
            kind = entry.get("type")
            found[entry["key"]] = convert_mapped(entry.get("value"), kind if isinstance(kind, str) else None)
    return found


class SandboxKind(_Kind):
    """``sandbox`` (node type ``script``): the code runs in the sandbox service with the resolved
    ``inputs`` as ``input``; the returned object is the output, any other value ``{value}``. The
    blueprint refuses the node type unless ``features.scriptNode`` is on; with the flag but no
    sandbox service the node is ``FEATURE_DISABLED``."""

    async def execute(self, ctx: NodeContext, node_input: NodeInput) -> NodeOutcome:
        sandbox = ctx.services.ports.sandbox
        if sandbox is None:
            raise ZonicError("FEATURE_DISABLED", feature="sandbox", node_id=self.spec.node_id)
        config = self.spec.config
        outcome = await sandbox.run(
            SandboxRequest(
                language=str(getattr(config, "language", None) or _DEFAULT_LANGUAGE),
                code=str(getattr(config, "code", None) or ""),
                input=mapping_record(node_input.get(_INPUTS)),
                timeout_seconds=self.spec.timeout_seconds
                or ctx.services.resources.manifest.defaults.timeout_seconds,
            )
        )
        result = outcome.result
        return NodeOutcome(
            output={"result": dict(result) if isinstance(result, Mapping) else {"value": result}},
            activate=self.default(ctx),
        )


_HTML_TEMPLATE, _TEMPLATE_CONFIG = "html_template", "template_config"
_GRID, _TABLE, _CHART = "grid", "table", "chart"
DATA_MISSING = "view.data-missing"


def _text(value: Any) -> str:
    if value is None:
        return ""
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)


class ViewKind(_Kind):
    """``view``: content parts from the builder's view configuration, each also sent as a
    ``content.part`` event and kept in the turn's answer message.

    * ``html_template`` (or a view with only an ``htmlTemplate``): the template, its values
      HTML-escaped by the port binding (``encode: html``), sanitized into an ``html`` part.
    * ``template_config`` ``grid``: a ``grid`` part over the dataset ``cacheDataKey`` names (a
      ``table`` part for rows inline); ``chart``: a ``chart`` part from ``chart_data``, checked, and
      an ``html`` part from ``html_data``. Another view type is ``CONTENT_INVALID``.

    The output is ``html`` (when there is one) and ``fields`` (``viewType``, ``datasetRef``)."""

    async def execute(self, ctx: NodeContext, node_input: NodeInput) -> NodeOutcome:
        content = ctx.services.settings.content
        node_id = self.spec.node_id
        data = node_input.get("data")
        if isinstance(data, str):
            data = parse_json(data, node_id) if data.strip() else None
        view = data if isinstance(data, Mapping) else {}
        view_type = view.get("viewType")
        mode = getattr(self.spec.config, "view_mode", None) or (
            _TEMPLATE_CONFIG if isinstance(view_type, str) else _HTML_TEMPLATE
        )
        parts: list[Any] = []
        fields: dict[str, Any] = {}
        warnings: list[dict[str, Any]] = []
        html: str | None = None

        def missing(message: str) -> None:
            warnings.append({"code": DATA_MISSING, "message": message, "nodeId": node_id})

        if mode == _HTML_TEMPLATE:
            html = sanitize_html(_text(node_input.get("html")), content)
            parts.append(html_part(html))
        else:
            kind = view_type if isinstance(view_type, str) else _GRID
            fields["viewType"] = kind
            if kind == _CHART:
                option = view.get("chart_data", view.get("option"))
                if option in (None, ""):
                    missing("the chart has no data")
                else:
                    ref, _ = view_data(view.get("cacheDataKey"))
                    parts.append(chart_part(checked_chart_option(option, content, node_id), ref))
                text = _text(view.get("html_data"))
                if text.strip():
                    html = sanitize_html(text, content)
                    parts.append(html_part(html))
            elif kind in (_GRID, _TABLE):
                part = grid_parts(view, content)
                ref = getattr(part, "dataset_ref", None)
                # A cache key a tool or node of this turn registered names its dataset (P3-05).
                datasets = ctx.run.ports.datasets
                if ref is not None and ref.dataset_id is None and datasets is not None:
                    meta = next((m for m in datasets.pending.metadata if m.get("cacheKey") == ref.key), None)
                    if meta is not None:
                        ref = ref.model_copy(update={"dataset_id": meta["datasetId"]})
                        part = part.model_copy(update={"dataset_ref": ref})
                parts.append(part)
                if ref is not None:
                    fields["datasetRef"] = ref.wire()
                elif part.type == _GRID:
                    missing("the grid has no dataset")
            else:
                raise ZonicError("CONTENT_INVALID", node_id=node_id, reason=f"view type {kind}")

        plain = [part.wire() for part in parts]
        for part in plain:
            ctx.emit("content.part", type=part["type"], part=part)
        output: dict[str, Any] = {"fields": fields}
        if html is not None:
            output["html"] = html
        return NodeOutcome(output=output, activate=self.default(ctx), parts=plain, warnings=warnings)
