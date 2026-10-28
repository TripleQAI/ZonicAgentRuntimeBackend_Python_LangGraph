"""The graph input of a turn (design 7.1, 9.2): ``system``, ``flow``, ``history`` and sub-flow ``input``.

The turn loader writes before the first superstep, so its writes carry step ``-1`` and no writer
(DR-1): any node's write of the same key wins over them.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

TURN_STEP, TURN_WRITER = -1, ""


def keyed(values: Mapping[str, Any]) -> dict[str, Any]:
    """A keyed channel (``flow`` or ``system``) holding ``values`` as the turn loader's writes."""
    return {
        "values": dict(values),
        "provenance": {key: [TURN_STEP, TURN_WRITER] for key in values},
        "conflicts": [],
    }


def turn_input(
    *,
    system: Mapping[str, Any],
    flow: Mapping[str, Any] | None = None,
    history: Mapping[str, Sequence[Mapping[str, Any]]] | None = None,
    inputs: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    state: dict[str, Any] = {"system": keyed(system), "flow": keyed(flow or {})}
    if history:
        state["history"] = {thread: [dict(m) for m in messages] for thread, messages in history.items()}
    if inputs:
        state["input"] = dict(inputs)
    return state
