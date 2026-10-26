"""SP-10: ``RunControl`` drain under ``durability="exit"`` and resume of the saved checkpoint
in another process (the fallback is releasing the run and restarting the turn).

Questions:
1. ``request_drain()`` mid-run stops the root graph at a superstep boundary with
   ``GraphDrained``, and the TurnSaver holds a checkpoint a new process continues from,
   with no completed step repeated.
2. The same while a child graph invoked inside a node is running (the child inherits the
   control): which steps of the child are repeated on resume.
3. Tasks in flight when the drain is requested finish before the run stops.
4. How long the drain takes relative to the step in progress.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest
from langgraph.errors import GraphDrained
from langgraph.runtime import RunControl

from spikes.sp10_graphs import FLOWS, STEP_S
from spikes.turnsaver import TurnSaver

ROOT = Path(__file__).resolve().parents[1]


def _resume(tmp_path: Path, flow: str, bundle: dict[str, Any]) -> list[str]:
    path = tmp_path / f"{flow}.json"
    path.write_text(json.dumps(bundle), encoding="utf-8")
    env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(ROOT / "src"), str(ROOT)])}
    done = subprocess.run(
        [sys.executable, "-m", "spikes.sp10_resume", flow, str(path)],
        capture_output=True,
        text=True,
        cwd=ROOT,
        env=env,
        timeout=120,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout.strip().splitlines()[-1])["log"]


async def _drain_after(flow: str, delay: float) -> tuple[TurnSaver, float, str]:
    saver = TurnSaver()
    control = RunControl()
    graph = FLOWS[flow](saver)
    config = {"configurable": {"thread_id": f"sp10-{flow}"}}
    task = asyncio.create_task(graph.ainvoke({"log": []}, config, durability="exit", control=control))
    await asyncio.sleep(delay)
    requested = time.perf_counter()
    control.request_drain("shutdown")
    with pytest.raises(GraphDrained) as info:
        await task
    return saver, time.perf_counter() - requested, info.value.reason


@pytest.mark.parametrize(
    ("flow", "delay", "expected"),
    [
        ("root", STEP_S * 2.5, [f"r{i}" for i in range(1, 7)]),
        ("child", STEP_S * 3.5, ["before", "sub", "c1", "c2", "c3", "c4", "c5", "after"]),
        ("parallel", STEP_S * 1.5, ["fork", "slow_a", "slow_b", "join"]),
    ],
)
async def test_drain_then_resume_in_a_new_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, flow: str, delay: float, expected: list[str]
) -> None:
    trace = tmp_path / "trace.txt"
    monkeypatch.setenv("SP10_TRACE", str(trace))
    saver, took, reason = await _drain_after(flow, delay)
    state = saver.storage[f"sp10-{flow}"]
    checkpoints = {ns or "<root>": len(ids) for ns, ids in state.items()}
    bundle = saver.export_bundle(f"sp10-{flow}")
    log = _resume(tmp_path, flow, bundle)
    runs: dict[str, int] = {}
    for line in trace.read_text(encoding="utf-8").splitlines():
        runs[line.split()[1]] = runs.get(line.split()[1], 0) + 1
    repeated = sorted(name for name, n in runs.items() if n > 1)
    print(
        f"\nSP-10 {flow}: drained ({reason}) {took * 1000:.0f} ms after the request "
        f"(step {STEP_S * 1000:.0f} ms); checkpoints {checkpoints}; resumed log {log}; "
        f"steps executed twice across the two processes: {repeated or 'none'}"
    )
    assert sorted(log) == sorted(expected)


@pytest.mark.parametrize(("durability", "repeats"), [("exit", True), ("async", False), ("sync", False)])
async def test_child_drain_by_durability(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, durability: str, repeats: bool
) -> None:
    """Which durability keeps a child graph's progress across a drain."""
    trace = tmp_path / "trace.txt"
    monkeypatch.setenv("SP10_TRACE", str(trace))
    saver, control = TurnSaver(), RunControl()
    config = {"configurable": {"thread_id": f"sp10-child-{durability}"}}
    task = asyncio.create_task(
        FLOWS["child"](saver).ainvoke({"log": []}, config, durability=durability, control=control)
    )
    await asyncio.sleep(STEP_S * 3.5)
    control.request_drain()
    with pytest.raises(GraphDrained):
        await task
    counts = {
        ns or "<root>": len(ids) for ns, ids in saver.storage[config["configurable"]["thread_id"]].items()
    }
    restored = TurnSaver.from_bundle(saver.export_bundle(config["configurable"]["thread_id"]))
    await FLOWS["child"](restored).ainvoke(None, config, durability=durability)
    names = [line.split()[1] for line in trace.read_text(encoding="utf-8").splitlines()]
    repeated = sorted({n for n in names if names.count(n) > 1})
    print(
        f"\nSP-10 child drain, durability={durability}: checkpoints {counts}; repeated {repeated or 'none'}"
    )
    assert bool(repeated) is repeats
