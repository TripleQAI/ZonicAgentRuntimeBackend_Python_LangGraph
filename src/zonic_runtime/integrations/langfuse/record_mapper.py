"""``LangfuseRecordMapper``: finished spans to ``sp_obs_ingest`` records (design 12; case O-01).

Every exported span is an ``ObsObservationRecord``: its ID is the span ID and its trace ID the
trace's, so a batch sent twice inserts nothing new (O-02). The run's root span also gives the
trace header ``ObsTraceRecord`` (name, session, user, tenant, workflow, tags, metadata, status).

Sources, in order:
* the Langfuse attributes (``langfuse.observation.*``, ``langfuse.trace.*``);
* the ``gen_ai.*`` attributes of other instrumentations, through ``genaiAttributeMap`` in
  ``observability.json``, where the Langfuse ones are missing;
* the node metadata the blueprint puts on every node span (``zonic_node_*``), promoted to the
  node columns and left out of ``metadata``, with the keys every span repeats from the trace.

Values the database types as UUIDs (run, session, workflow) are sent only when they are UUIDs and
kept in ``metadata`` otherwise. An input or output above ``limits.maxObservationBytes`` is
replaced by its summary. No cost is computed here: ``sp_obs_ingest`` prices the usage (O-04).
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Any, get_args

from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.trace import StatusCode

from zonic_runtime.integrations.langfuse.export import INPUT, LEVEL, META, OUTPUT, RUN_ROOT, STATUS, TYPE
from zonic_runtime.integrations.langfuse.masker import summary
from zonic_runtime.models.observability import ObservationType
from zonic_runtime.models.resources.misc import ObservabilityConfig

TRACE_META = "langfuse.trace.metadata."
TRACE_NAME, TRACE_TAGS = "langfuse.trace.name", "langfuse.trace.tags"
USER, SESSION = "user.id", "session.id"
ENVIRONMENT, RELEASE, VERSION = "langfuse.environment", "langfuse.release", "langfuse.version"
MODEL = "langfuse.observation.model.name"
MODEL_PARAMETERS = "langfuse.observation.model.parameters"
USAGE, COST = "langfuse.observation.usage_details", "langfuse.observation.cost_details"
COMPLETION_START = "langfuse.observation.completion_start_time"
STATUS_META = "zonic_status"

_TYPES = frozenset(get_args(ObservationType))
#: Metadata keys promoted to their own columns.
_PROMOTED = frozenset(
    {
        "zonic_node_id",
        "zonic_node_type",
        "zonic_node_name",
        "zonic_kind",
        "zonic_visit",
        "zonic_path",
        "zonic_hook_binding_id",
        "zonic_observation_type",
        "zonic_node_span",
        "zonic_error_handler",
        RUN_ROOT,
        STATUS_META,
    }
)
#: Keys every span repeats from the trace or from LangChain's bookkeeping.
_REPEATED = frozenset(
    {
        "zonic_run_id",
        "zonic_workflow_id",
        "zonic_revision",
        "thread_id",
        "environment",
        "tags",
        "ls_integration",
        "lc_versions",
        "checkpoint_ns",
        "langgraph_checkpoint_ns",
        "langgraph_triggers",
        "langgraph_path",
        "is_langchain_root",
    }
)
#: ``genaiAttributeMap`` targets -> record fields.
_GENAI_FIELDS = MappingProxyType(
    {"model": "model", "usage_details": "usageDetails", "model_parameters": "modelParameters"}
)
_WORKFLOW_KEY, _TENANT_KEY = "agenticWorkflowId", "tenantId"
_MAX_NAME = 200
_MAX_STATUS = 500
_NS_PER_MS = 1_000_000
_IN, _OUT = "input", "output"  # principle: allow rule-1-node-type record-fields


def _json(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


def _time(ns: int | None) -> str | None:
    if ns is None:
        return None
    moment = datetime.fromtimestamp(ns / 1e9, tz=UTC)
    return moment.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _uuid(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        return str(uuid.UUID(value))
    except ValueError:
        return None


def _prefixed(attrs: Mapping[str, Any], prefix: str) -> dict[str, Any]:
    return {k[len(prefix) :]: _json(v) for k, v in attrs.items() if k.startswith(prefix)}


def _mapping(value: Any) -> dict[str, Any] | None:
    found = _json(value)
    return dict(found) if isinstance(found, Mapping) else None


def _put(record: dict[str, Any], key: str, value: Any) -> None:
    if value is not None:
        record[key] = value


def observation_id(span: ReadableSpan) -> str:
    return format(span.context.span_id if span.context is not None else 0, "016x")


def trace_hex(span: ReadableSpan) -> str:
    return format(span.context.trace_id if span.context is not None else 0, "032x")


class LangfuseRecordMapper:
    def __init__(self, config: ObservabilityConfig) -> None:
        self._max_bytes = config.limits.max_observation_bytes
        self._genai = dict(config.genai_attribute_map)

    def records(self, spans: Sequence[ReadableSpan]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """``(traces, observations)`` of the spans, in their order."""
        traces = [self.trace(s) for s in spans if _json((s.attributes or {}).get(META + RUN_ROOT))]
        return traces, [self.observation(s) for s in spans]

    # -- the trace header ---------------------------------------------------------------------

    def trace(self, root: ReadableSpan) -> dict[str, Any]:
        attrs = root.attributes or {}
        metadata = _prefixed(attrs, TRACE_META)
        own = _prefixed(attrs, META)
        record: dict[str, Any] = {"traceId": trace_hex(root)}
        run_id = _uuid(metadata.get("zonic_run_id"))
        _put(record, "runId", run_id)
        _put(record, "name", str(attrs.get(TRACE_NAME) or root.name)[:_MAX_NAME])
        _put(record, "environment", attrs.get(ENVIRONMENT))
        _put(record, "release", attrs.get(RELEASE))
        _put(record, "version", attrs.get(VERSION))
        _put(record, "sessionId", _uuid(attrs.get(SESSION)))
        _put(record, "userId", attrs.get(USER))
        _put(record, "tenantId", metadata.get(_TENANT_KEY))
        _put(record, "agenticWorkflowId", _uuid(metadata.get(_WORKFLOW_KEY)))
        tags = attrs.get(TRACE_TAGS)
        record["tags"] = sorted({str(t) for t in tags}) if isinstance(tags, (list, tuple)) else []
        kept = {k: v for k, v in metadata.items() if k not in _REPEATED}
        if record.get("sessionId") is None and attrs.get(SESSION) is not None:
            kept["sessionId"] = attrs.get(SESSION)
        record["metadata"] = kept
        _put(record, _IN, self._capped(_json(attrs.get(INPUT))))
        _put(record, _OUT, self._capped(_json(attrs.get(OUTPUT))))
        _put(record, "status", own.get(STATUS_META) or attrs.get(STATUS))
        _put(record, "startTime", _time(root.start_time))
        _put(record, "endTime", _time(root.end_time))
        return record

    # -- observations ---------------------------------------------------------------------------

    def observation(self, span: ReadableSpan) -> dict[str, Any]:
        attrs = span.attributes or {}
        metadata = _prefixed(attrs, META)
        root = bool(metadata.get(RUN_ROOT))
        kind = attrs.get(TYPE)
        failed = span.status.status_code == StatusCode.ERROR
        record: dict[str, Any] = {
            "observationId": observation_id(span),
            "traceId": trace_hex(span),
            "type": kind if kind in _TYPES else "span",
            "name": span.name[:_MAX_NAME],
            "level": attrs.get(LEVEL) or ("ERROR" if failed else "DEFAULT"),
            "startTime": _time(span.start_time),
        }
        if span.parent is not None and not root:  # the root's parent is the propagated context
            record["parentObservationId"] = format(span.parent.span_id, "016x")
        status = attrs.get(STATUS) or (span.status.description if failed else None)
        _put(record, "statusMessage", str(status)[:_MAX_STATUS] if status else None)
        _put(record, "environment", attrs.get(ENVIRONMENT))
        _put(record, "version", attrs.get(VERSION))
        _put(record, "endTime", _time(span.end_time))
        if span.end_time is not None and span.start_time is not None:
            record["latencyMs"] = max(0, (span.end_time - span.start_time) // _NS_PER_MS)
        _put(record, "completionStartTime", _json(attrs.get(COMPLETION_START)))
        truncated = []
        for key, part in ((INPUT, _IN), (OUTPUT, _OUT)):
            value = _json(attrs.get(key))
            capped = self._capped(value)
            if capped is not value:
                truncated.append(part)
            _put(record, part, capped)
        self._model(record, attrs)
        self._node(record, metadata)
        kept = {k: v for k, v in metadata.items() if k not in _PROMOTED and k not in _REPEATED}
        if truncated:
            kept["truncated"] = truncated
        record["metadata"] = kept
        return record

    def _model(self, record: dict[str, Any], attrs: Mapping[str, Any]) -> None:
        _put(record, "model", attrs.get(MODEL))
        _put(record, "modelParameters", _mapping(attrs.get(MODEL_PARAMETERS)))
        _put(record, "usageDetails", _mapping(attrs.get(USAGE)))
        cost = _mapping(attrs.get(COST))
        if cost is not None:
            record["costDetails"] = cost
            total = cost.get("total")
            if isinstance(total, (int, float)):
                record["totalCost"] = total
        for attribute, target in self._genai.items():  # other instrumentations' gen_ai.* attributes
            value = attrs.get(attribute)
            if value is None:
                continue
            field, _, key = target.partition(".")
            column = _GENAI_FIELDS.get(field)
            if column is None:
                continue
            if not key:
                record.setdefault(column, value)
            else:
                record.setdefault(column, {}).setdefault(key, value)

    @staticmethod
    def _node(record: dict[str, Any], metadata: Mapping[str, Any]) -> None:
        for column, key in (
            ("nodeId", "zonic_node_id"),
            ("nodeType", "zonic_node_type"),
            ("nodeName", "zonic_node_name"),
            ("nodeKind", "zonic_kind"),
            ("path", "zonic_path"),
            ("hookBindingId", "zonic_hook_binding_id"),
        ):
            value = metadata.get(key)
            if value is not None:
                record[column] = str(value)
        visit = metadata.get("zonic_visit")
        if isinstance(visit, int):
            record["visit"] = visit

    def _capped(self, value: Any) -> Any:
        if value is None:
            return None
        size = len(json.dumps(value, ensure_ascii=False, default=str).encode("utf-8"))
        return value if size <= self._max_bytes else summary(value)
