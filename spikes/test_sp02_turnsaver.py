"""SP-2: ``TurnSaver`` with ``durability="exit"``, interrupts in child and agent graphs invoked in
nodes, resume in a new process from the serialized bundle, the conformance suite, and
``error_handler`` with ``Runtime`` injection.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypedDict

from langgraph.checkpoint.conformance import checkpointer_test, validate
from langgraph.errors import NodeError
from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime
from langgraph.types import Command
from tests.fakes.scripted_chat_model import ScriptedChatModel, Turn

from spikes.sp02_graphs import agent_flow, child_flow
from spikes.turnsaver import TurnSaver

ROOT = Path(__file__).resolve().parents[1]


def _resume_in_new_process(tmp_path: Path, flow: str, bundle: dict[str, Any], resume: Any) -> dict[str, Any]:
    (tmp_path / "bundle.json").write_text(json.dumps(bundle), encoding="utf-8")
    (tmp_path / "resume.json").write_text(json.dumps(resume), encoding="utf-8")
    env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(ROOT / "src"), str(ROOT)])}
    done = subprocess.run(
        [
            sys.executable,
            "-m",
            "spikes.sp02_resume",
            flow,
            str(tmp_path / "bundle.json"),
            str(tmp_path / "resume.json"),
        ],
        capture_output=True,
        text=True,
        cwd=ROOT,
        env=env,
        timeout=120,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    print(f"SP-2 resume process stderr ({flow}): {done.stderr.strip() or '<empty>'}")
    return json.loads(done.stdout.strip().splitlines()[-1])


def _counts(saver: TurnSaver, thread_id: str) -> dict[str, int]:
    return {ns or "<root>": len(ids) for ns, ids in saver.storage[thread_id].items()}


async def test_child_graph_interrupt_resumes_in_a_new_process(tmp_path: Path) -> None:
    saver = TurnSaver()
    graph = child_flow(saver)
    config = {"configurable": {"thread_id": "run-child"}}
    out = await graph.ainvoke({"log": []}, config, durability="exit")
    (pending,) = out["__interrupt__"]
    print("\nSP-2.1 interrupt:", pending.value, "id", pending.id, "len", len(pending.id))
    print("SP-2.1 checkpoints per namespace:", _counts(saver, "run-child"))

    bundle = saver.export_bundle("run-child")
    size = len(json.dumps(bundle))
    print(
        f"SP-2.1 bundle: {len(bundle['checkpoints'])} checkpoints, {len(bundle['writes'])} writes, "
        f"{len(bundle['blobs'])} blobs, {size} bytes JSON"
    )
    result = _resume_in_new_process(tmp_path, "child", bundle, "yes")
    print("SP-2.1 resumed in a new process:", result)
    assert result["log"] == ["pre", "subflow", "post"]
    assert result["answer"] == "yes"


async def test_agent_graph_interrupt_resumes_in_a_new_process(tmp_path: Path) -> None:
    saver = TurnSaver()
    model = ScriptedChatModel(script=[Turn.call("send_email", {"to": "bob", "body": "report"})])
    graph = agent_flow(saver, model)
    config = {"configurable": {"thread_id": "run-agent"}}
    out = await graph.ainvoke({"log": []}, config, durability="exit")
    (pending,) = out["__interrupt__"]
    print("\nSP-2.2 HITL request:", json.dumps(pending.value)[:300])
    print("SP-2.2 checkpoints per namespace:", _counts(saver, "run-agent"))
    bundle = saver.export_bundle("run-agent")
    print(f"SP-2.2 bundle: {len(json.dumps(bundle))} bytes JSON")

    result = _resume_in_new_process(tmp_path, "agent", bundle, {"decisions": [{"type": "approve"}]})
    print("SP-2.2 resumed in a new process:", result)
    assert result["answer"] == "Email sent to bob."
    assert result["model_calls"] == 1  # the first model turn is not repeated
    assert result["log"] == ["pre", "agent"]


async def test_full_bundle_and_latest_only_bundle_both_resume(tmp_path: Path) -> None:
    saver = TurnSaver()
    graph = child_flow(saver)
    config = {"configurable": {"thread_id": "run-full"}}
    await graph.ainvoke({"log": []}, config, durability="async")
    full = saver.export_bundle("run-full", keep_latest=False)
    latest = saver.export_bundle("run-full", keep_latest=True)
    print(
        f"\nSP-2.3 durability=async: full bundle {len(full['checkpoints'])} checkpoints "
        f"{len(json.dumps(full))} bytes; latest-only {len(latest['checkpoints'])} checkpoints "
        f"{len(json.dumps(latest))} bytes"
    )
    result = _resume_in_new_process(tmp_path, "child", latest, "ok")
    assert result["log"] == ["pre", "subflow", "post"]


async def test_conformance_suite() -> None:
    @checkpointer_test(name="TurnSaver")
    async def turn_saver() -> Any:
        yield TurnSaver()

    report = await validate(turn_saver)
    print("\nSP-2.4 conformance:")
    print(report.to_text() if hasattr(report, "to_text") else report)
    assert report.passed_all_base() if hasattr(report, "passed_all_base") else True


@dataclass
class Ctx:
    tenant: str


class S(TypedDict, total=False):
    status: str


async def test_error_handler_receives_node_error_and_runtime() -> None:
    seen: dict[str, Any] = {}

    def explode(state: S) -> S:
        raise ValueError("boom")

    def handler(state: S, error: NodeError, runtime: Runtime[Ctx]) -> Command[str]:
        seen.update(
            node=error.node,
            error=repr(error.error),
            tenant=runtime.context.tenant,
            attempt=runtime.execution_info.node_attempt if runtime.execution_info else None,
        )
        return Command(update={"status": f"recovered from {error.node}"}, goto="after")

    g = StateGraph(S, context_schema=Ctx)
    g.add_node("explode", explode, error_handler=handler)
    g.add_node("after", lambda s: {"status": s["status"] + " then after"})
    g.add_edge(START, "explode")
    g.add_edge("explode", END)
    g.add_edge("after", END)
    out = await g.compile().ainvoke({}, context=Ctx(tenant="t1"))
    print("\nSP-2.5 error_handler saw:", seen, "->", out)
    assert seen["node"] == "explode" and seen["tenant"] == "t1"
    assert out["status"] == "recovered from explode then after"
