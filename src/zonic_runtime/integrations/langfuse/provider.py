"""``LangfuseTracer``: one trace per run on an isolated tracer provider (design 12; plan P2-07, SP-4).

The tracer provider is the runtime's own, never the global one. The Langfuse client exports
through it to the given span exporter (the gateway exporter of P2-08; an in-memory exporter in
tests) behind the ``SamplingExporter``, with ``mask`` (the ``Masker``) and ``mask_otel_spans`` (the
``Enricher``). Nothing is sent to a Langfuse server.

A run's ``LangfuseRunTrace`` wraps the run in its root observation, whose trace ID is the run ID
(case L-01), under ``propagate_attributes`` (trace name, session, user, tags, metadata), and gives
the run its callback handler and event bridge. The root observation's metadata also carries the
workflow's graph (``workflow_graph``: its nodes with type, kind and model, and its edges with their
handles), and each node visit can record its bound input (``node_input``). While a run is active,
library warnings go to its trace (``observability/logging_policy.py``).
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from typing import Any

from langfuse import Langfuse, propagate_attributes
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SpanExporter

from zonic_runtime.config.env import NO_LANGFUSE_SERVER
from zonic_runtime.integrations.langfuse.callback_handler import (
    ObservationBridge,
    ZonicLangfuseCallbackHandler,
)
from zonic_runtime.integrations.langfuse.export import INPUT_BOUND, RUN_ROOT, Enricher, SamplingExporter
from zonic_runtime.integrations.langfuse.masker import Masker
from zonic_runtime.integrations.langfuse.record_mapper import STATUS_META
from zonic_runtime.models.blueprint import IGNORED_KIND
from zonic_runtime.models.resources.catalogs import ModelCatalog
from zonic_runtime.models.resources.misc import ObservabilityConfig
from zonic_runtime.models.resources.settings import ObservabilitySettings
from zonic_runtime.observability.logging_policy import CURRENT_RUN_EVENTS
from zonic_runtime.ports.tracer import Level, RunTrace, TraceInfo

_FAILED = frozenset({"failed", "timed_out"})
_SPAN = "span"
_ANNOTATION = "zonic_annotation"
_TRACE_ID_DIGITS = 32
GRAPH = "zonic_workflow_graph"


def trace_id(run_id: str) -> str:
    """The run ID as a trace ID (case L-01): a UUID's 32 hexadecimal digits; any other ID is hashed."""
    digits = run_id.replace("-", "").lower()
    if len(digits) == _TRACE_ID_DIGITS and all(c in "0123456789abcdef" for c in digits):
        return digits
    return hashlib.sha256(run_id.encode()).hexdigest()[:_TRACE_ID_DIGITS]


def workflow_graph(blueprint: Any) -> dict[str, Any] | None:
    """The workflow as compiled: its nodes (swarm members with their ``container``) and its
    edges by source handle, for the trace's root observation."""
    if blueprint is None:
        return None
    nodes = []
    for spec in blueprint.nodes.values():
        if not spec.reachable or spec.kind == IGNORED_KIND:
            continue
        node: dict[str, Any] = {
            "id": spec.node_id,
            "type": spec.node_type,
            "kind": spec.kind,
            "name": spec.name,
        }
        model = getattr(spec.config, "model", None)  # a code, or the normalized ``{modelId: ...}``
        model = model.get("modelId") if isinstance(model, dict) else model
        if isinstance(model, str) and model:
            node["model"] = model
        if spec.container:
            node["container"] = spec.container
        nodes.append(node)
    edges = [
        {"from": source, "handle": handle, "to": target}
        for source, handles in blueprint.edge_index.edges.items()
        for handle, targets in handles.items()
        for target in targets
    ]
    return {
        "workflowId": blueprint.workflow_id,
        "revision": blueprint.revision,
        "entry": blueprint.entry,
        "nodes": nodes,
        "edges": edges,
    }


class LangfuseRunTrace:
    def __init__(self, tracer: LangfuseTracer, info: TraceInfo) -> None:
        self._tracer = tracer
        self.info = info
        self._handler = ZonicLangfuseCallbackHandler(
            public_key=tracer.public_key, blueprint=info.blueprint, children=info.children
        )
        self._bridge = ObservationBridge(self._handler)
        self._root: Any = None

    @property
    def callbacks(self) -> list[Any]:
        return [self._handler]

    @contextmanager
    def active(self) -> Iterator[None]:
        info = self.info
        metadata = dict(info.metadata)
        graph = workflow_graph(info.blueprint)
        trace_context: Any = {"trace_id": trace_id(info.run_id)}
        with (
            self._tracer.client.start_as_current_observation(
                as_type="span",
                name=info.name,
                trace_context=trace_context,
                input=self._tracer.masker.mask(info.input),
                metadata={**metadata, RUN_ROOT: True, **({GRAPH: graph} if graph else {})},
            ) as root,
            propagate_attributes(
                trace_name=info.name,
                session_id=info.session_id,
                user_id=info.user_id,
                tags=list(info.tags),
                metadata={k: str(v) for k, v in metadata.items()},
            ),
        ):
            self._root = root
            token = CURRENT_RUN_EVENTS.set(self.run_event)
            try:
                yield
            finally:
                CURRENT_RUN_EVENTS.reset(token)
                self._root = None

    def event(
        self, config: Any, name: str, level: Level = "DEFAULT", metadata: Mapping[str, Any] | None = None
    ) -> None:
        self._bridge.event(config, name, level, self._tracer.masker.mask(dict(metadata or {})), self._root)

    def span(
        self, config: Any, name: str, level: Level = "DEFAULT", metadata: Mapping[str, Any] | None = None
    ) -> None:
        masked = self._tracer.masker.mask(dict(metadata or {}))
        self._bridge.event(config, name, level, masked, self._root, as_type=_SPAN)

    def node_input(self, config: Any, value: Mapping[str, Any]) -> None:
        """The node visit's bound port values as its observation's input; the export captures them
        as the node type's rule says (``full``, ``summary`` or ``none``)."""
        target = self._bridge.parent(config, None)
        if target is not None:
            target.update(input=self._tracer.masker.mask(dict(value)), metadata={INPUT_BOUND: True})

    def annotate(
        self,
        config: Any,
        tags: Sequence[str] = (),
        metadata: Mapping[str, Any] | None = None,
        score: Mapping[str, Any] | None = None,
    ) -> None:
        """Tags and metadata go on the observation (``zonic_annotation``); a score becomes an
        ``ObsScoreRecord`` the exporter ingests (``sp_obs_ingest``)."""
        target = self._bridge.parent(config, self._root) if config is not None else self._root
        if target is None:
            return
        annotation = {"tags": list(tags), **self._tracer.masker.mask(dict(metadata or {}))}
        target.update(metadata={_ANNOTATION: annotation})
        if score:
            context = target._otel_span.get_span_context()
            observation = None if config is None else format(context.span_id, "016x")
            self._tracer.score(format(context.trace_id, "032x"), observation, self.info.session_id, score)

    def run_event(self, name: str, level: str, metadata: Mapping[str, Any]) -> None:
        if self._root is not None:
            self._root.start_observation(
                name=name, as_type="event", level=level, metadata=self._tracer.masker.mask(dict(metadata))
            )

    def end(self, status: str, output: Any = None, error_code: str | None = None) -> None:
        if self._root is None:
            return
        level = "ERROR" if status in _FAILED else "WARNING" if status == "cancelled" else "DEFAULT"
        self._root.update(
            output=self._tracer.masker.mask(output),
            level=level,
            status_message=error_code or status,
            metadata={STATUS_META: status},  # the trace header's status (P2-08)
        )


class LangfuseTracer:
    def __init__(
        self,
        settings: ObservabilitySettings,
        config: ObservabilityConfig,
        exporter: SpanExporter,
        *,
        release: str = "dev",
        prices: ModelCatalog | None = None,
    ) -> None:
        """``prices``: the model catalog that costs generations (``Enricher``)."""
        self.masker = Masker(config)
        self.public_key = f"pk-lf-zonic-{uuid.uuid4().hex[:16]}"
        self.provider = TracerProvider()
        self.exporter = SamplingExporter(exporter, settings.sample_rate, settings.always_trace_errors)
        self.client = Langfuse(
            public_key=self.public_key,
            secret_key="sk-lf-zonic",  # unused: nothing is sent to a Langfuse server
            base_url=NO_LANGFUSE_SERVER,
            environment=settings.environment,
            release=release,
            sample_rate=1.0,  # sampling is the SamplingExporter's, after the run's outcome is known
            mask=self.masker,
            mask_otel_spans=Enricher(self.masker, prices),
            tracer_provider=self.provider,
            span_exporter=self.exporter,
            flush_interval=settings.export.interval_milliseconds / 1000,
        )

    def begin(self, info: TraceInfo) -> RunTrace:
        return LangfuseRunTrace(self, info)

    def score(
        self, trace: str, observation: str | None, session: str | None, score: Mapping[str, Any]
    ) -> None:
        """An ``annotate`` score, as an ``ObsScoreRecord``, to the exporter when it ingests scores."""
        value = score.get("value")
        if isinstance(value, bool):
            kind = "BOOLEAN"
        elif isinstance(value, (int, float)):
            kind = "NUMERIC"
        else:
            kind = "CATEGORICAL"
        record: dict[str, Any] = {
            "scoreId": str(uuid.uuid4()),
            "traceId": trace,
            "name": str(score.get("name") or ""),
            "dataType": kind,
            "source": "API",
        }
        if kind == "CATEGORICAL":
            record["stringValue"] = str(value)
        else:
            record["value"] = float(value)  # type: ignore[arg-type]
        for key, found in (
            ("observationId", observation),
            ("sessionId", session),
            ("comment", score.get("comment")),
        ):
            if found:
                record[key] = found
        sink = getattr(self.exporter.inner, "score", None)
        if callable(sink):
            sink(record)

    def flush(self) -> None:
        self.client.flush()

    def shutdown(self) -> None:
        self.client.shutdown()
        self.provider.shutdown()
