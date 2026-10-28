"""What leaves the process, decided at export (design 12; plan P2-07, SP-4, DR-5).

* ``Enricher`` (Langfuse's ``mask_otel_spans``): a node span is typed from the manifest's
  ``observation`` block (``zonic_observation_type``, case L-03), never from the handler's name
  heuristic. Its ``Command`` output is unwrapped: the node record's output, captured as the node
  type's rule says, replaces it, and a failed record (an error policy's decision) sets ``WARNING``
  with the code (DR-5, L-17). Model and tool spans inside a node follow its type's capture rule
  (case L-10).
* ``SamplingExporter``: ``observability.sampleRate`` decided per trace when the run's root span
  ends, so a run that failed is exported whatever the rate (``alwaysTraceErrors``, case L-12).
  Holding the whole trace until then also lets it drop the agent graph's internal ``model`` and
  ``tools`` wrapper spans (``wrapper``), which only duplicate their children, and give those
  children the wrapper's parent (SP-4, ``tidy``): a model call hangs under its agent node.
"""

from __future__ import annotations

import json
import threading
from collections import OrderedDict
from collections.abc import Mapping, Sequence
from typing import Any

from langfuse.types import MaskOtelSpansParams, MaskOtelSpansResult, OtelSpanPatch
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult
from opentelemetry.trace import SpanContext

from zonic_runtime.integrations.langfuse.callback_handler import NODE_SPAN
from zonic_runtime.integrations.langfuse.masker import Masker

TYPE = "langfuse.observation.type"
LEVEL = "langfuse.observation.level"
STATUS = "langfuse.observation.status_message"
INPUT, OUTPUT = "langfuse.observation.input", "langfuse.observation.output"
META = "langfuse.observation.metadata."
RUN_ROOT = "zonic_run_root"
_WRAPPERS = frozenset({"model", "tools"})
_CHAIN_TYPES = frozenset({"chain", "agent", "span"})  # principle: allow rule-1-node-type langfuse-types
_FAILED = "failed"
#: The parts of a span the capture rules name, and the ``Command`` key of a node's update.
_INPUT_PART, _OUTPUT_PART = "input", "output"  # principle: allow rule-1-node-type capture-parts
_UPDATE = "update"  # principle: allow rule-5-sql command-update-key
_NONE = "none"
_MAX_PENDING_TRACES = 2000


def _json(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


def meta(span: Any, key: str) -> Any:
    return _json((span.attributes or {}).get(META + key))


def wrapper(span: ReadableSpan) -> bool:
    """The agent graph's ``model`` or ``tools`` node: a chain span around its model or tool calls."""
    attrs = span.attributes or {}
    named = span.name in _WRAPPERS and meta(span, "langgraph_node") in _WRAPPERS
    return named and attrs.get(TYPE) in _CHAIN_TYPES and not meta(span, NODE_SPAN)


def _reparented(span: ReadableSpan, parent: SpanContext | None) -> ReadableSpan:
    return ReadableSpan(
        name=span.name,
        context=span.context,
        parent=parent,
        resource=span.resource,
        attributes=span.attributes,
        events=span.events,
        links=span.links,
        kind=span.kind,
        status=span.status,
        start_time=span.start_time,
        end_time=span.end_time,
        instrumentation_scope=span.instrumentation_scope,
    )


def tidy(spans: Sequence[ReadableSpan]) -> list[ReadableSpan]:
    """Without the wrapper spans; their children get the wrapper's own parent."""
    dropped = {s.context.span_id: s.parent for s in spans if s.context is not None and wrapper(s)}
    if not dropped:
        return list(spans)
    kept: list[ReadableSpan] = []
    for span in spans:
        if span.context is not None and span.context.span_id in dropped:
            continue
        parent = span.parent
        while parent is not None and parent.span_id in dropped:
            parent = dropped[parent.span_id]
        kept.append(span if parent is span.parent else _reparented(span, parent))
    return kept


class Enricher:
    def __init__(self, masker: Masker) -> None:
        self._masker = masker

    def _captured(
        self, attrs: Mapping[str, Any], key: str, mode: str, value: Any = None
    ) -> tuple[dict[str, Any], tuple[str, ...]]:
        if mode == _NONE:
            return {}, (key,) if key in attrs else ()
        found = value if value is not None else _json(attrs.get(key))
        if found is None:
            return {}, ()
        return {key: json.dumps(self._masker.captured(found, mode), ensure_ascii=False, default=str)}, ()

    def patch(self, span: Any) -> OtelSpanPatch | None:
        attrs = span.attributes or {}
        set_attributes: dict[str, Any] = {}
        delete: tuple[str, ...] = ()
        status = attrs.get(STATUS)
        if isinstance(status, str) and self._masker.text(status) != status:  # an error's text (X-05)
            set_attributes[STATUS] = self._masker.text(status)
        node_type = meta(span, "zonic_node_type")
        if node_type is None:
            return OtelSpanPatch(set_attributes=set_attributes) if set_attributes else None
        node_span = bool(meta(span, NODE_SPAN))
        if node_span:
            set_attributes[TYPE] = meta(span, "zonic_observation_type") or "span"
            output = _json(attrs.get(OUTPUT))
            update = output.get(_UPDATE) if isinstance(output, Mapping) else None
            record = (
                (update or {}).get("nodes", {}).get(meta(span, "zonic_node_id"))
                if isinstance(update, Mapping)
                else None
            )
            if isinstance(record, Mapping):
                mode = self._masker.capture(node_type, _OUTPUT_PART)
                found, gone = self._captured(attrs, OUTPUT, mode, record.get(_OUTPUT_PART) or {})
                set_attributes.update(found)
                delete += gone
                if record.get("status") == _FAILED and attrs.get(LEVEL) != "ERROR":
                    set_attributes[LEVEL] = "WARNING"
                    set_attributes[STATUS] = str((record.get("error") or {}).get("code") or _FAILED)
            # The node's input is the run state: a summary at most.
            mode = self._masker.capture(node_type, _INPUT_PART)
            found, gone = self._captured(attrs, INPUT, _NONE if mode == _NONE else "summary")
            set_attributes.update(found)
            delete += gone
        elif attrs.get(TYPE) not in _CHAIN_TYPES:  # a model or tool call inside the node
            for key, part in ((INPUT, _INPUT_PART), (OUTPUT, _OUTPUT_PART)):
                mode = self._masker.capture(node_type, part)
                if mode != "full":
                    found, gone = self._captured(attrs, key, mode)
                    set_attributes.update(found)
                    delete += gone
        if not set_attributes and not delete:
            return None
        return OtelSpanPatch(set_attributes=set_attributes, delete_attributes=delete)

    def __call__(self, *, params: MaskOtelSpansParams) -> MaskOtelSpansResult:
        patches = {}
        for identifier, span in params.spans.items():
            patch = self.patch(span)
            if patch is not None:
                patches[identifier] = patch
        return MaskOtelSpansResult(span_patches=patches)


def _sampled(trace_id: int, rate: float) -> bool:
    return rate >= 1.0 or (trace_id % 10_000) < rate * 10_000


class SamplingExporter(SpanExporter):
    """Holds a trace's spans until its root span ends, then exports all or none of them.
    Runs on the span processor's worker thread, hence the lock."""

    def __init__(self, inner: SpanExporter, rate: float, always_errors: bool) -> None:
        self._inner = inner
        #: The exporter behind sampling (the gateway exporter ingests ``annotate`` scores).
        self.inner = inner
        self._rate = rate
        self._always_errors = always_errors
        self._lock = threading.Lock()
        self._pending: OrderedDict[int, list[ReadableSpan]] = OrderedDict()
        self._decided: OrderedDict[int, bool] = OrderedDict()

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        ready: list[ReadableSpan] = []
        with self._lock:
            for span in spans:
                trace_id = span.context.trace_id if span.context is not None else 0
                decided = self._decided.get(trace_id)
                if decided is not None:
                    if decided:
                        ready.append(span)
                    continue
                held = self._pending.setdefault(trace_id, [])
                held.append(span)
                if meta(span, RUN_ROOT):
                    failed = any((s.attributes or {}).get(LEVEL) == "ERROR" for s in held)
                    keep = _sampled(trace_id, self._rate) or (self._always_errors and failed)
                    self._remember(trace_id, keep)
                    held = self._pending.pop(trace_id)
                    if keep:
                        ready += held
            while len(self._pending) > _MAX_PENDING_TRACES:  # a root that never ended: drop its spans
                self._pending.popitem(last=False)
        ready = tidy(ready)
        return self._inner.export(ready) if ready else SpanExportResult.SUCCESS

    def _remember(self, trace_id: int, keep: bool) -> None:
        self._decided[trace_id] = keep
        while len(self._decided) > _MAX_PENDING_TRACES:
            self._decided.popitem(last=False)

    def shutdown(self) -> None:
        self._inner.shutdown()

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        return self._inner.force_flush(timeout_millis)


class FanOutExporter(SpanExporter):
    """The same spans to the gateway exporter and to the optional Langfuse server exporter
    (``observability.exporters`` with ``langfuse``, plan P3-11, case O-08). The gateway's result
    is the result: a failure of the second exporter, whatever it raises, never reaches the first,
    so PostgreSQL ingest is unaffected by it."""

    def __init__(self, primary: SpanExporter, secondary: SpanExporter) -> None:
        self.primary, self.secondary = primary, secondary
        #: Exports the second exporter failed (counted, never raised).
        self.secondary_failures = 0

    def score(self, record: dict[str, Any]) -> None:
        sink = getattr(self.primary, "score", None)
        if callable(sink):
            sink(record)

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        result = self.primary.export(spans)
        try:
            if self.secondary.export(spans) != SpanExportResult.SUCCESS:
                self.secondary_failures += 1
        except Exception:  # noqa: BLE001  # the optional exporter never affects the gateway's
            self.secondary_failures += 1
        return result

    def shutdown(self) -> None:
        self.primary.shutdown()
        try:
            self.secondary.shutdown()
        except Exception:  # noqa: BLE001  # as above
            self.secondary_failures += 1

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        done = self.primary.force_flush(timeout_millis)
        try:
            self.secondary.force_flush(timeout_millis)
        except Exception:  # noqa: BLE001  # as above
            self.secondary_failures += 1
        return done
