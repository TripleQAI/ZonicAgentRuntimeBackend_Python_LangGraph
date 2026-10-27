"""``RunState``: the LangGraph state of one run, and its reducers (design 7.1; plan P1-08).

Every namespace has a reducer, so parallel writers never raise ``InvalidUpdateError``, and
every reducer gives the same value whatever order LangGraph applies a superstep's writes
in (DR-1; LangGraph does not guarantee that order):

* ``flow`` and ``system`` are keyed channels ``{values, provenance, conflicts}``. Each write
  carries its superstep and writer node ID; per key the highest ``(step, writer)`` wins.
  Two writers of one key in the same superstep are a conflict: the higher node ID is kept
  and the conflict is recorded, which the template reports as a ``state.conflict``
  warning. The resolver reads only ``values``.
* ``result`` keeps the lowest ``(step, writer)``: the first Output wins, later ones are
  listed in ``ignored``.
* List namespaces (``turn_messages``, ``timeline``, ``datasets``, ``warnings``, ``errors``)
  hold entries with an order key ``_seq = [step, writer, index]`` and stay sorted by it.
* ``nodes`` keeps the record with the highest visit per node; ``visits`` and ``usage`` add.

Values are plain JSON (rule 15: built from the class models), so the checkpoint
serializer needs no pickle fallback.
"""

from __future__ import annotations

import bisect
from collections.abc import Iterable, Mapping, Sequence
from typing import Annotated, Any, TypedDict

#: The order key of a list entry: ``[step, writer, index]``.
SEQ = "_seq"
#: ``visits`` key of the run-wide node execution count.
RUN_VISITS = "__run__"
#: A ``turn_messages`` entry that hides earlier messages of a thread (state operation ``clear``).
CLEAR_THREAD = "clearThread"


# -- keyed channels: flow and system ------------------------------------------------------


def empty_keyed() -> dict[str, Any]:
    return {"values": {}, "provenance": {}, "conflicts": []}


def _order(step: int, writer: str) -> tuple[int, str]:
    return (step, writer)


def merge_keyed(left: Mapping[str, Any] | None, right: Mapping[str, Any] | None) -> dict[str, Any]:
    """``left``: the channel. ``right``: a patch ``{step, writer, set, remove}``, or a whole
    channel (the turn loader's initial value)."""
    channel: dict[str, Any] = (
        empty_keyed()
        if left is None
        else {
            "values": dict(left.get("values", {})),
            "provenance": dict(left.get("provenance", {})),
            "conflicts": list(left.get("conflicts", [])),
        }
    )
    if right is None:
        return channel
    if "values" in right:  # a whole channel: merge it as writes of its own provenance
        for key, value in right["values"].items():
            step, writer = right.get("provenance", {}).get(key, (-1, ""))
            _write(channel, key, value, step, writer, removed=False)
        for conflict in right.get("conflicts", []):
            if conflict not in channel["conflicts"]:
                channel["conflicts"].append(conflict)
        _sort_conflicts(channel)
        return channel
    step, writer = int(right["step"]), str(right["writer"])
    for key, value in (right.get("set") or {}).items():
        _write(channel, key, value, step, writer, removed=False)
    for key in right.get("remove") or []:
        _write(channel, key, None, step, writer, removed=True)
    _sort_conflicts(channel)
    return channel


def _write(channel: dict[str, Any], key: str, value: Any, step: int, writer: str, *, removed: bool) -> None:
    values, provenance = channel["values"], channel["provenance"]
    current = provenance.get(key)
    if current is not None:
        current_step, current_writer = current[0], current[1]
        if current_step == step and current_writer != writer:
            writers = sorted({current_writer, writer, *_conflict_writers(channel, key, step)})
            _record_conflict(channel, key, step, writers)
        if _order(step, writer) < _order(current_step, current_writer):
            return
    provenance[key] = [step, writer]
    if removed:
        values.pop(key, None)
    else:
        values[key] = value


def _conflict_writers(channel: Mapping[str, Any], key: str, step: int) -> list[str]:
    for conflict in channel["conflicts"]:
        if conflict["key"] == key and conflict["step"] == step:
            return list(conflict["writers"])
    return []


def _record_conflict(channel: dict[str, Any], key: str, step: int, writers: list[str]) -> None:
    for conflict in channel["conflicts"]:
        if conflict["key"] == key and conflict["step"] == step:
            conflict["writers"] = writers
            conflict["kept"] = writers[-1]
            return
    channel["conflicts"].append({"key": key, "step": step, "writers": writers, "kept": writers[-1]})


def _sort_conflicts(channel: dict[str, Any]) -> None:
    channel["conflicts"] = sorted(
        ({**c, "writers": list(c["writers"])} for c in channel["conflicts"]),
        key=lambda c: (c["step"], c["key"]),
    )


def keyed_values(channel: Mapping[str, Any] | None) -> dict[str, Any]:
    return dict(channel.get("values", {})) if channel else {}


# -- result ---------------------------------------------------------------------------------


def first_result(left: Mapping[str, Any] | None, right: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """``{value, step, writer, ignored}``: the lowest ``(step, writer)`` wins."""
    if right is None:
        return dict(left) if left is not None else None
    if left is None:
        return {
            "value": right["value"],
            "step": right["step"],
            "writer": right["writer"],
            "ignored": sorted(right.get("ignored", [])),
        }
    candidates = [left, right]
    winner, loser = sorted(candidates, key=lambda r: _order(int(r["step"]), str(r["writer"])))
    ignored = {*left.get("ignored", []), *right.get("ignored", [])}
    if _order(int(loser["step"]), str(loser["writer"])) != _order(int(winner["step"]), str(winner["writer"])):
        ignored.add(str(loser["writer"]))
    return {
        "value": winner["value"],
        "step": winner["step"],
        "writer": winner["writer"],
        "ignored": sorted(ignored),
    }


# -- lists, records, counters ------------------------------------------------------------------


def _seq(entry: Mapping[str, Any]) -> tuple[int, str, int]:
    step, writer, index = entry[SEQ]
    return (int(step), str(writer), int(index))


def merge_entries(
    left: Sequence[Mapping[str, Any]] | None, right: Sequence[Mapping[str, Any]] | None
) -> list[Any]:
    """Entries sorted by ``_seq``; usually ``right`` comes after ``left`` and is appended."""
    merged = list(left or [])
    for entry in sorted(right or [], key=_seq):
        if not merged or _seq(merged[-1]) <= _seq(entry):
            merged.append(entry)
        else:
            keys = [_seq(e) for e in merged]
            merged.insert(bisect.bisect_right(keys, _seq(entry)), entry)
    return merged


def merge_nodes(left: Mapping[str, Any] | None, right: Mapping[str, Any] | None) -> dict[str, Any]:
    """Node records by node ID; per node the record with the highest visit is kept."""
    merged = dict(left or {})
    for node_id, record in (right or {}).items():
        current = merged.get(node_id)
        if current is None or int(record.get("visit", 0)) >= int(current.get("visit", 0)):
            merged[node_id] = record
    return merged


def add_counts(left: Mapping[str, int] | None, right: Mapping[str, int] | None) -> dict[str, int]:
    merged = dict(left or {})
    for key, count in (right or {}).items():
        merged[key] = merged.get(key, 0) + int(count)
    return merged


_USAGE_INTS = ("inputTokens", "outputTokens", "totalTokens", "modelCalls")


def add_usage(left: Mapping[str, Any] | None, right: Mapping[str, Any] | None) -> dict[str, Any]:
    merged: dict[str, Any] = {
        key: int((left or {}).get(key, 0)) + int((right or {}).get(key, 0)) for key in _USAGE_INTS
    }
    costs = [c for c in ((left or {}).get("cost"), (right or {}).get("cost")) if c is not None]
    if costs:
        merged["cost"] = round(sum(float(c) for c in costs), 9)
    return merged


class RunState(TypedDict, total=False):
    system: Annotated[dict[str, Any], merge_keyed]
    flow: Annotated[dict[str, Any], merge_keyed]
    input: dict[str, Any]  # sub-flow inputs, written once
    history: dict[str, list[dict[str, Any]]]  # by thread, read at turn start; read-only
    nodes: Annotated[dict[str, dict[str, Any]], merge_nodes]
    turn_messages: Annotated[list[dict[str, Any]], merge_entries]
    visits: Annotated[dict[str, int], add_counts]
    timeline: Annotated[list[dict[str, Any]], merge_entries]
    datasets: Annotated[list[dict[str, Any]], merge_entries]
    result: Annotated[dict[str, Any] | None, first_result]
    warnings: Annotated[list[dict[str, Any]], merge_entries]
    errors: Annotated[list[dict[str, Any]], merge_entries]
    usage: Annotated[dict[str, Any], add_usage]


# -- readers ----------------------------------------------------------------------------------


def without_seq(entries: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Entries as their models dump them (the order key removed)."""
    return [{k: v for k, v in e.items() if k != SEQ} for e in entries]
