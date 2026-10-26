"""SP-2 resume in a new process: ``python -m spikes.sp02_resume <flow> <bundle.json> <resume.json>``.

Loads the bundle into a fresh ``TurnSaver``, rebuilds the graph, resumes with
``Command(resume=...)`` under ``durability="exit"`` and prints the result as JSON.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

from langgraph.types import Command
from tests.fakes.scripted_chat_model import ScriptedChatModel, Turn

from spikes.sp02_graphs import agent_flow, child_flow
from spikes.turnsaver import TurnSaver


async def main(flow: str, bundle_path: str, resume_path: str) -> dict[str, Any]:
    bundle = json.loads(Path(bundle_path).read_text(encoding="utf-8"))
    resume = json.loads(Path(resume_path).read_text(encoding="utf-8"))
    saver = TurnSaver.from_bundle(bundle)
    model = ScriptedChatModel(script=[Turn.say("Email sent to bob.")])
    graph = child_flow(saver) if flow == "child" else agent_flow(saver, model)
    config = {"configurable": {"thread_id": bundle["threadId"]}}
    out = await graph.ainvoke(Command(resume=resume), config, durability="exit")
    return {"log": out["log"], "answer": out["answer"], "model_calls": len(model.requests)}


if __name__ == "__main__":
    print(json.dumps(asyncio.run(main(*sys.argv[1:4]))))
