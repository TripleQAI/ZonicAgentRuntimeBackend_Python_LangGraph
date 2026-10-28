"""LangGraph stream events to run event drafts (design Section 10; plan P2-01, DR-11).

``EventMapper`` is the one place that reads LangGraph's event shapes. Both sources feed it:
the v3 ``RunEventTransformer`` and the v2 fallback mapper, so they publish the same events
(case S-12). Each mapper belongs to one run and is dropped with it.

* ``custom`` events from the node template (``runtime.stream_writer``) carry their node ID;
  the namespace adds the enclosing nodes as ``path``. A Swarm's member steps write their
  ``swarm.*`` events from inside the Swarm's subgraph: the Swarm node itself is not on its path.
* ``messages`` text deltas become ``token.delta`` of the node whose graph produced them: the
  namespace's last segment, since an agent's own ``model`` and ``tools`` nodes add no segment
  (SP-3).
* ``tools`` events become ``tool.started`` and ``tool.finished``.

A namespace segment is ``<graph node name>:<task id>``; names are escaped so they hold no
``:`` (SP-9), and the blueprint maps them back to builder node IDs. A Swarm member's segment
(a node of the Swarm's child blueprint with the Swarm as its ``container``) is not a node of its
own: its text and tool events are the Swarm node's, with ``member`` (P4-01, case W-08).
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, cast, get_args

from zonic_runtime.models.blueprint import Blueprint
from zonic_runtime.models.engine import Usage
from zonic_runtime.models.events import EventDraft, RunEventType
from zonic_runtime.runtime.redact import StreamMasker, StreamMaskRules
from zonic_runtime.workflow.templates.resolver import OUTPUT_FIELD

#: Event types a node (or the swarm wrapper) may emit through the custom stream.
RUN_EVENT_TYPES = frozenset(get_args(RunEventType))
#: Envelope fields of the template's custom payload; the rest is the event's data.
_ENVELOPE = frozenset({"event", "nodeId", "nodeType", "name", "visit"})
#: Protocol methods the mapper reads.
CUSTOM, MESSAGES, TOOLS = "custom", "messages", "tools"
_TEXT_DELTA, _REASONING_DELTA = "text-delta", "reasoning-delta"
_BLOCK_DELTA = "content-block-delta"
_CHECKPOINT_NS, _NS_SEP = "langgraph_checkpoint_ns", "|"
_TOOL_STARTED, _TOOL_FINISHED, _TOOL_ERROR = "tool-started", "tool-finished", "tool-error"
_ERROR_STATUS = "error"
#: The ``ToolMessage`` field of LangGraph's ``tool-finished`` event.
_TOOL_OUTPUT = "output"  # principle: allow rule-1-node-type langgraph-tools-event-key
#: ``NodeFinishedData.output_summary`` comes from the template's ``output``.
_OUTPUT, _OUTPUT_SUMMARY = OUTPUT_FIELD, "outputSummary"

Summarize = Callable[[Any], dict[str, Any]]


def segment_name(segment: str) -> str:
    return segment.split(":", 1)[0]


#: ``(parent blueprint, node ID) -> the child blueprint it opened`` (a sub-flow's, a Swarm's members).
ChildLookup = Callable[[Blueprint, str], Blueprint | None]
#: Kinds whose namespace continues in a child blueprint they open at their visit.
_CONTAINERS = frozenset({"subflow", "multi_agent"})


@dataclass(frozen=True)
class Place:
    """Where a namespace is: the builder node IDs down to it, and the Swarm member, if any."""

    ids: tuple[str, ...]
    member: str | None = None


class NamespaceMap:
    """Namespace paths to builder node IDs. Child blueprints (sub-flows, P3-09; a Swarm's members,
    P4-01) are looked up by the node ID that runs them. A segment no blueprint knows (an agent
    graph's own ``model`` and ``tools`` nodes) is not a builder node and is skipped."""

    def __init__(self, root: Blueprint, children: ChildLookup | None = None) -> None:
        self._root = root
        self._children: ChildLookup = children or (lambda _parent, _node: None)
        self._cache: dict[tuple[str, ...], Place] = {}

    def resolve(self, namespace: Sequence[str]) -> tuple[str, ...]:
        return self.place(namespace).ids

    def place(self, namespace: Sequence[str]) -> Place:
        key = tuple(namespace)
        found = self._cache.get(key)
        if found is None:
            ids: list[str] = []
            member: str | None = None
            blueprint: Blueprint | None = self._root
            complete = True
            for segment in key:
                spec = blueprint.by_graph_name(segment_name(segment)) if blueprint is not None else None
                if blueprint is None or spec is None:
                    blueprint = None
                    continue
                if ids and spec.container == ids[-1]:  # a Swarm member: its events are the Swarm's
                    member, blueprint = spec.name, None
                    continue
                ids.append(spec.node_id)
                child = self._children(blueprint, spec.node_id)
                complete = complete and (child is not None or spec.kind not in _CONTAINERS)
                blueprint = child
            found = Place(tuple(ids), member)
            if complete:  # a container whose child is not open yet is not cached as unknown
                self._cache[key] = found
        return found


@dataclass(frozen=True)
class StreamMasking:
    """How the text a node streams is masked: its ``mask`` bindings' patterns (P3-10)."""

    rules: Callable[[str], StreamMaskRules | None]
    patterns: Mapping[str, str]
    holdback_chars: int


_DELTAS: tuple[RunEventType, ...] = ("token.delta", "reasoning.delta")


class EventMapper:
    def __init__(
        self,
        names: NamespaceMap,
        summarize: Summarize,
        *,
        reasoning: bool = False,
        monotonic: Callable[[], float] = time.monotonic,
        masking: StreamMasking | None = None,
    ) -> None:
        self.names = names
        self._summarize = summarize
        self._reasoning = reasoning
        self._monotonic = monotonic
        self._masking = masking
        #: The maskers of nodes with ``mask`` bindings, by node and delta type; ``None`` otherwise.
        self._maskers: dict[tuple[str, str], StreamMasker | None] = {}
        self._tool_starts: dict[str, float] = {}
        #: Usage of every finished node so far, for runs that end without a final state.
        self.usage = Usage()

    def map(self, method: str, namespace: Sequence[str], data: Any) -> list[EventDraft]:
        if method == CUSTOM:
            return self.custom(namespace, data)
        if method == MESSAGES:
            return self.message(namespace, data)
        if method == TOOLS:
            return self.tool(namespace, data)
        return []

    def _place(self, namespace: Sequence[str]) -> tuple[str | None, str | None, dict[str, str]]:
        """The node a namespace belongs to, the nodes enclosing it, and its Swarm member tag."""
        place = self.names.place(namespace)
        if not place.ids:
            return None, None, {}
        member = {"member": place.member} if place.member else {}
        return place.ids[-1], "/".join(place.ids[:-1]) or None, member

    def custom(self, namespace: Sequence[str], payload: Any) -> list[EventDraft]:
        if not isinstance(payload, Mapping):
            return []
        event = payload.get("event")
        if event not in RUN_EVENT_TYPES:
            return []
        node_id = payload.get("nodeId")
        if isinstance(node_id, str):
            ids = self.names.resolve(namespace)
            if ids and ids[-1] == node_id:  # written inside the node's own subgraph (a Swarm member)
                ids = ids[:-1]
            path = "/".join(ids) or None
        else:
            node_id, path, _ = self._place(namespace)
        data = {k: v for k, v in payload.items() if k not in _ENVELOPE}
        if event == "node.started":
            data = {
                "nodeType": payload.get("nodeType"),
                "nodeName": payload.get("name"),
                "visit": payload.get("visit"),
                **data,
            }
        elif event == "node.finished":
            if _OUTPUT in data:
                data[_OUTPUT_SUMMARY] = self._summarize(data.pop(_OUTPUT))
            usage = data.get("usage")
            if isinstance(usage, Mapping):
                self.usage = self.usage + Usage.model_validate(usage)
        data = {k: v for k, v in data.items() if v is not None}
        found = EventDraft(type=cast(RunEventType, event), data=data, node_id=node_id, path=path)
        if event == "node.finished" and isinstance(node_id, str):  # held-back text goes out first
            return [*self._flush(node_id, path), found]
        return [found]

    def _masker(self, node_id: str, kind: str) -> StreamMasker | None:
        """The masker of one node's text or reasoning stream, created on its first delta."""
        key = (node_id, kind)
        if key not in self._maskers:
            rules = self._masking.rules(node_id) if self._masking is not None else None
            self._maskers[key] = (
                StreamMasker(
                    rules.patterns, self._masking.patterns, rules.replacement, self._masking.holdback_chars
                )
                if rules is not None and self._masking is not None
                else None
            )
        return self._maskers[key]

    def _flush(self, node_id: str, path: str | None) -> list[EventDraft]:
        """A masked node's held-back text; its maskers start afresh on its next visit."""
        drafts = []
        for kind in _DELTAS:
            masker = self._maskers.pop((node_id, kind), None)
            rest = masker.flush() if masker is not None else ""
            if rest:
                drafts.append(EventDraft(type=kind, data={"text": rest}, node_id=node_id, path=path))
        return drafts

    @staticmethod
    def model_namespace(namespace: Sequence[str], metadata: Any) -> Sequence[str]:
        """Where a model call ran: its checkpoint namespace (``node:task|...``) names the node
        that called it even at the root, where the event's own namespace is empty."""
        found = metadata.get(_CHECKPOINT_NS) if isinstance(metadata, Mapping) else None
        return found.split(_NS_SEP) if isinstance(found, str) and found else namespace

    def text(self, namespace: Sequence[str], text: str, *, reasoning: bool = False) -> list[EventDraft]:
        if not text or (reasoning and not self._reasoning):
            return []
        node_id, path, member = self._place(namespace)
        kind: RunEventType = "reasoning.delta" if reasoning else "token.delta"
        masker = self._masker(node_id, kind) if node_id is not None else None
        if masker is not None:
            text = masker.push(text)
            if not text:
                return []
        return [EventDraft(type=kind, data={"text": text, **member}, node_id=node_id, path=path)]

    def message(self, namespace: Sequence[str], data: Any) -> list[EventDraft]:
        event = data[0] if isinstance(data, (tuple, list)) and data else data
        if not isinstance(event, Mapping) or event.get("event") != _BLOCK_DELTA:
            return []
        if isinstance(data, (tuple, list)) and len(data) > 1:
            namespace = self.model_namespace(namespace, data[1])
        delta = event.get("delta") or {}
        kind = delta.get("type")
        if kind == _TEXT_DELTA:
            return self.text(namespace, str(delta.get("text") or ""))
        if kind == _REASONING_DELTA:
            return self.text(
                namespace, str(delta.get("reasoning") or delta.get("text") or ""), reasoning=True
            )
        return []

    def tool_started(
        self, namespace: Sequence[str], tool_use_id: str, tool_name: str, tool_input: Any
    ) -> list[EventDraft]:
        self._tool_starts[tool_use_id] = self._monotonic()
        node_id, path, member = self._place(namespace)
        data = {
            "toolName": tool_name,
            "toolUseId": tool_use_id,
            "inputSummary": self._summarize(tool_input),
            **member,
        }
        return [EventDraft(type="tool.started", data=data, node_id=node_id, path=path)]

    def tool_finished(self, namespace: Sequence[str], tool_use_id: str, failed: bool) -> list[EventDraft]:
        started = self._tool_starts.pop(tool_use_id, None)
        node_id, path, member = self._place(namespace)
        data: dict[str, Any] = {
            "toolUseId": tool_use_id,
            "status": "error" if failed else "success",
            **member,
        }
        if started is not None:
            data["durationMs"] = max(0.0, (self._monotonic() - started) * 1000)
        return [EventDraft(type="tool.finished", data=data, node_id=node_id, path=path)]

    def tool(self, namespace: Sequence[str], data: Any) -> list[EventDraft]:
        if not isinstance(data, Mapping):
            return []
        event = data.get("event")
        tool_use_id = str(data.get("tool_call_id") or "")
        if event == _TOOL_STARTED:
            return self.tool_started(
                namespace, tool_use_id, str(data.get("tool_name") or ""), data.get("input")
            )
        if event in (_TOOL_FINISHED, _TOOL_ERROR):
            output = data.get(_TOOL_OUTPUT)
            failed = event == _TOOL_ERROR or getattr(output, "status", None) == _ERROR_STATUS
            return self.tool_finished(namespace, tool_use_id, failed)
        return []
