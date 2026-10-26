"""SP-10 resume in a new process: ``python -m spikes.sp10_resume <flow> <bundle.json>``.

Loads the drained run's bundle, rebuilds the graph and continues it with ``ainvoke(None)``.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

from spikes.sp10_graphs import FLOWS
from spikes.turnsaver import TurnSaver


async def main(flow: str, bundle_path: str) -> dict[str, Any]:
    bundle = json.loads(Path(bundle_path).read_text(encoding="utf-8"))
    graph = FLOWS[flow](TurnSaver.from_bundle(bundle))
    out = await graph.ainvoke(None, {"configurable": {"thread_id": bundle["threadId"]}}, durability="exit")
    return {"log": out["log"]}


if __name__ == "__main__":
    print(json.dumps(asyncio.run(main(*sys.argv[1:3]))))
