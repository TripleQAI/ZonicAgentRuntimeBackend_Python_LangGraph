"""The invocation configuration of a run (design 7.3; plan P1-09, case G-12)."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from zonic_runtime.engine.context import RunIdentity
from zonic_runtime.models.resources.settings import EngineSettings


def invocation_config(
    run: RunIdentity,
    engine: EngineSettings,
    *,
    environment: str,
    callbacks: Sequence[Any] = (),
    child: bool = False,
) -> dict[str, Any]:
    """``thread_id`` is the run ID; ``recursion_limit`` is the superstep backstop of the visit
    limits (design 6.6); ``max_concurrency`` bounds one graph, the run permits bound the run."""
    return {
        "configurable": {"thread_id": run.run_id},
        "recursion_limit": engine.child_supersteps if child else engine.supersteps,
        "max_concurrency": engine.max_concurrency_per_graph,
        "callbacks": list(callbacks),
        "metadata": {
            "zonic_run_id": run.run_id,
            "zonic_workflow_id": run.workflow_id,
            "zonic_revision": run.revision,
            "environment": environment,
        },
        "tags": [f"workflow:{run.workflow_id}", f"mode:{run.mode}"],
    }
