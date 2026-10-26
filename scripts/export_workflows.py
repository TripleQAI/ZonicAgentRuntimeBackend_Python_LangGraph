"""Export workflows from the product database into ``tests/fixtures/workflows/builder/open-data/``.

Reads ``agent_builder.v_agent_summary`` (to find each workflow by name) and
``agent_builder.v_agent_export`` through the gateway, as the runtime's caller
(``ZONIC_GATEWAY_BASE_URL`` and ``ZONIC_GATEWAY_API_KEY``, from the environment or
``.env``), and records each file's SHA-256 in ``SOURCE.json``. The default names are
the three open-data FSH agents of plan Section 20.1.

    uv run python -m scripts.export_workflows [name ...]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Any

from scripts.sync_builder import MANIFEST, TARGET, _dump, _sha256
from zonic_runtime.config.env import EnvironmentModel
from zonic_runtime.config.settings import load_settings
from zonic_runtime.integrations.gateway.http_gateway import HttpGateway
from zonic_runtime.models.gateway.wire import Filter

DEFAULT_NAMES = ("fsh-energy-agent", "fsh-facility-agent", "fsh-risk-agent")
SOURCE = "agent-build-langgraph: agent_builder.v_agent_export (gateway datasource agentruntime)"


async def export(names: list[str]) -> dict[str, Any]:
    env = EnvironmentModel()
    gateway = HttpGateway(load_settings(env.resources_dir, env.env).gateway, env)
    written: dict[str, Any] = {}
    try:
        for name in names:
            found = await gateway.view(
                "runtime", "builder", "v_agent_summary", filters=[Filter(column="name", value=name)]
            )
            if found.row_count != 1:
                raise SystemExit(f"{name}: expected one workflow, found {found.row_count}")
            workflow_id = found.rows[0]["agentic_workflow_id"]
            export_row = await gateway.view(
                "runtime",
                "builder",
                "v_agent_export",
                filters=[Filter(column="agentic_workflow_id", value=workflow_id)],
            )
            data = _dump(export_row.rows[0]["export_json"])
            path = f"open-data/{name}.json"
            (TARGET / path).parent.mkdir(parents=True, exist_ok=True)
            (TARGET / path).write_bytes(data)
            written[path] = {"source": f"{SOURCE}, name {name}", "sha256": _sha256(data)}
    finally:
        await gateway.aclose()
    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("names", nargs="*", default=list(DEFAULT_NAMES))
    args = parser.parse_args(argv)
    written = asyncio.run(export(args.names))
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    manifest["files"] = dict(sorted({**manifest["files"], **written}.items()))
    MANIFEST.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(f"exported {len(written)} workflows: {', '.join(sorted(written))}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
