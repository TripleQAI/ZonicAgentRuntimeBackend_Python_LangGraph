"""The MCP pool against a real Zonic MCP server (opt-in: ``uv run pytest -m live``; P3-01).

Needs ``ZONIC_MCP_BASE_URL`` (for example a local ``ZonicMCPServer_FastAPI`` on port 8300) and the
server's ``FUNCTION_ALLOWLIST`` to include ``statistics``. Only a function tool is called: no
database and no outbound request.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from pydantic import SecretStr

from tests.live.conftest import live_environment
from zonic_runtime.config.settings import load_resources
from zonic_runtime.integrations.mcp.execute import McpService
from zonic_runtime.integrations.mcp.pool import McpPool
from zonic_runtime.models.mcp import McpExecuteTool
from zonic_runtime.ports.mcp import RunMcp

pytestmark = pytest.mark.live

ENV = live_environment()
MEAN = McpExecuteTool(
    id="mean",
    type="function",
    config={"functionPath": "statistics.mean", "callStyle": "kwargs"},
    input={"data": [1, 2, 6]},
)


@pytest.fixture
async def service() -> AsyncIterator[McpService]:
    if not ENV.mcp_base_url:
        pytest.skip("ZONIC_MCP_BASE_URL is not set")

    def read(field: str) -> str | None:
        value = getattr(ENV, field, None)
        return value.get_secret_value() if isinstance(value, SecretStr) else value

    found = McpService(McpPool(load_resources(ENV).mcp_servers, read))
    yield found
    await found.aclose()


async def test_live_the_server_lists_its_execute_tool_over_one_held_connection(service: McpService) -> None:
    tools = await service.tools()
    assert "execute" in {t.name for t in tools}
    await service.tools()
    assert service.pool.connections == {"zonic": 1}


async def test_live_execute_runs_a_function_tool_and_reports_a_failing_one(service: McpService) -> None:
    bound = RunMcp(service, "live-run", "live-langgraph-verify")
    result = await bound.execute("fn:1", [MEAN])
    assert result.status == "success"
    # every tool type answers in the same envelope; a function binds no flow variable
    assert result.results["mean"].output == {"is_cached": False, "data": {"variable": None, "value": 3}}
    assert dict(result.flow_updates) == {}
    broken = MEAN.model_copy(update={"id": "nope", "config": {"functionPath": "statistics.no_such_function"}})
    partial = await bound.execute("fn:2", [MEAN, broken])
    assert (
        partial.status == "partial"
        and partial.results["nope"].status == "error"
        and partial.results["nope"].error
    )
