"""A test-only reference interpreter of the routing semantics (plan P1-09, case G-09).

Supersteps: every scheduled node runs once per step (two triggers in one step are one run,
case E-20); a barrier node waits until a step has no other node to run, then all waiting
barriers run together (LangGraph ``defer``, spike SP-1). Activated handles go through the
blueprint's edge index exactly as ``engine.routing`` does.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Sequence

from zonic_runtime.engine.routing import targets
from zonic_runtime.models.blueprint import Blueprint, NodeSpec

Activate = Callable[[NodeSpec, int], Sequence[str]]


def interpret(blueprint: Blueprint, activate: Activate, max_steps: int = 1000) -> Counter[str]:
    visits: Counter[str] = Counter()
    current = [blueprint.entry]
    waiting: list[str] = []
    for _ in range(max_steps):
        run_now = [n for n in current if not blueprint.nodes[n].join.barrier]
        waiting += [n for n in current if blueprint.nodes[n].join.barrier and n not in waiting]
        if not run_now:
            if not waiting:
                return visits
            run_now, waiting = sorted(waiting), []
        scheduled: list[str] = []
        for node_id in run_now:
            visits[node_id] += 1
            spec = blueprint.nodes[node_id]
            for target in targets(blueprint, spec, activate(spec, visits[node_id])):
                if target not in scheduled:
                    scheduled.append(target)
        current = scheduled
    raise AssertionError("the reference run did not terminate")
