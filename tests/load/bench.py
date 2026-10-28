"""In-process budgets of design 11.1 (plan P5-02): ``python -m tests.load.bench``.

Run inside the load container (1 vCPU, 2 GB) so the numbers are the target task's. The gateway is
the in-memory fake: these are the runtime's own costs, without network.

* Compilation of a 25-node document on a cache miss, and a cache hit.
* Per executed node, template and superstep together: a warm 200-node linear turn (Q-02) divided
  by its nodes.
* A Rule with 50 branches (Q-03): the turn's time, only the taken branch running.
"""

from __future__ import annotations

import asyncio
import json
import statistics
import sys
import time
from typing import Any

from tests.api.app import DEV, auth, make_app
from tests.api.test_persistence import wide
from tests.kinds.agents import agent_harness
from tests.kinds.harness import DEFAULT_INPUT
from zonic_runtime.workflow.compiler.graph_cache import GraphCache


def stats(values: list[float]) -> dict[str, float]:
    ordered = sorted(values)
    return {
        "p50": round(ordered[len(ordered) // 2], 3),
        "p95": round(ordered[min(len(ordered) - 1, int(0.95 * len(ordered)))], 3),
        "mean": round(statistics.fmean(values), 3),
    }


def compile_times(rounds: int = 30) -> dict[str, Any]:
    harness, _ = agent_harness([], DEV)
    blueprint = harness.plan(wide(23))  # Start, 23 nodes, Output: 25
    misses = []
    for _ in range(rounds):
        began = time.perf_counter()
        harness.compile(blueprint)
        misses.append((time.perf_counter() - began) * 1000)
    cache = GraphCache(lambda b, _: harness.compile(b), DEV.manifest, 16)
    cache.get(blueprint)
    hits = []
    for _ in range(rounds * 100):
        began = time.perf_counter()
        cache.get(blueprint)
        hits.append((time.perf_counter() - began) * 1000)
    return {"nodes": len(blueprint.nodes), "miss_ms": stats(misses), "hit_ms": stats(hits)}


async def per_node(rounds: int = 20) -> dict[str, Any]:
    document = wide(198)
    app = make_app(workflows={"wf-chat": document})
    body = {"agenticWorkflowId": "wf-chat", "input": {"text": "go"}, "mode": "sync"}
    totals = []
    async with app.client() as client:
        first = (await client.post("/v1/runs", json=body, headers=auth())).json()
        for _ in range(rounds):
            again = {**body, "sessionId": first["sessionId"]}
            began = time.perf_counter()
            result = (await client.post("/v1/runs", json=again, headers=auth())).json()
            totals.append((time.perf_counter() - began) * 1000)
            assert result["status"] == "succeeded", result
    nodes = len(document["nodes"])
    return {"nodes": nodes, "turn_ms": stats(totals), "per_node_ms": stats([t / nodes for t in totals])}


async def rule_fan_out(rounds: int = 20) -> dict[str, Any]:
    from tests.unit.test_capacity import fifty_branches

    harness, _ = agent_harness([], DEV)
    document = fifty_branches()
    inputs = {**DEFAULT_INPUT, "flow": {"values": {"n": 37}, "provenance": {}, "conflicts": []}}
    times = []
    for _ in range(rounds):
        began = time.perf_counter()
        final, *_ = await harness.run(document, inputs)
        times.append((time.perf_counter() - began) * 1000)
    ran = sorted(n for n in final["nodes"] if n.startswith("b"))
    return {"branches": 50, "ran": ran, "turn_ms": stats(times)}


async def main() -> int:
    report = {
        "compile": compile_times(),
        "per_node": await per_node(),
        "rule_fan_out": await rule_fan_out(),
    }
    sys.stdout.write(json.dumps(report, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
