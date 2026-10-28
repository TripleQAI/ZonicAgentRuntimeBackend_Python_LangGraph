"""Live sanity through a running pgfoundation gateway (opt-in: ``uv run pytest -m live``).

Drives the runtime's ``HttpGateway`` against the gateway named in ``tests/live/conftest.py``
(the deployed Lambda by default; datasources ``agentruntime`` and ``agentobservability``)
on a database with the agent_builder, agent_runtime and agent_observability schemas. It reads
every view, checks the procedures' reason tokens map to catalog codes over real HTTP, and
writes only idempotent partition bookkeeping; it creates no runtime data.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest

from tests.live.conftest import live_environment
from zonic_runtime.config.settings import load_settings
from zonic_runtime.errors import ZonicError
from zonic_runtime.integrations.gateway.http_gateway import HttpGateway
from zonic_runtime.integrations.gateway.repository import encode_argument
from zonic_runtime.models.gateway.wire import BatchItem

pytestmark = pytest.mark.live

ENV = live_environment()
SETTINGS = load_settings(ENV.resources_dir, ENV.env).gateway

RUNTIME_VIEWS = [
    "v_agent_catalog",
    "v_session_summary",
    "v_message",
    "v_run",
    "v_run_queue",
    "v_interrupt_pending",
    "v_session_state",
    "v_checkpoint",
    "v_dataset",
]
OBSERVABILITY_VIEWS = [
    "v_obs_trace",
    "v_obs_trace_list",
    "v_obs_observation",
    "v_obs_score",
    "v_obs_node_stats_daily",
    "v_obs_model_usage_daily",
]


@pytest.fixture
async def gateway() -> AsyncIterator[HttpGateway]:
    gw = HttpGateway(SETTINGS, ENV)
    if not await gw.ready():
        await gw.aclose()
        pytest.skip(f"no gateway ready at {ENV.gateway_base_url}")
    yield gw
    await gw.aclose()


@pytest.mark.parametrize("view", RUNTIME_VIEWS)
async def test_runtime_view_reads(gateway: HttpGateway, view: str) -> None:
    result = await gateway.view("runtime", "runtime", view, limit=1)
    assert result.row_count <= 1


@pytest.mark.parametrize("view", OBSERVABILITY_VIEWS)
async def test_observability_view_reads(gateway: HttpGateway, view: str) -> None:
    result = await gateway.view("observability", "observability", view, limit=1)
    assert result.row_count <= 1


async def test_builder_export_view_reads_through_the_runtime_datasource(gateway: HttpGateway) -> None:
    result = await gateway.view("runtime", "builder", "v_agent_export", limit=1)
    assert result.row_count <= 1


@pytest.mark.parametrize(
    ("procedure", "args", "code"),
    [
        ("sp_session_update", [str(uuid.uuid4()), "u-live", {"title": "x"}], "NOT_FOUND"),
        ("sp_run_transition", [str(uuid.uuid4()), "cancelled", {}], "NOT_FOUND"),
        ("sp_interrupt_answer", [str(uuid.uuid4()), "u-live", {"approved": True}], "NOT_FOUND"),
        ("sp_session_create", [{}, None], "DATA_REQUEST_INVALID"),
    ],
)
async def test_procedure_errors_map_to_catalog_codes(
    gateway: HttpGateway, procedure: str, args: list[object], code: str
) -> None:
    encoded = [encode_argument(a) for a in args]  # JSON as text, as GatewayRepository sends it
    with pytest.raises(ZonicError) as info:
        await gateway.procedure("runtime", "runtime", procedure, encoded)
    assert info.value.code == code


async def test_idempotent_write(gateway: HttpGateway) -> None:
    result = await gateway.procedure(
        "observability", "observability", "sp_obs_partition_ensure", [2, None], retry=True
    )
    assert result.rows and result.rows[0]["o_created"] == 0


async def test_transactional_batch(gateway: HttpGateway) -> None:
    item = {"schema": "observability", "name": "sp_obs_partition_ensure", "args": [2, None]}
    batch = await gateway.batch(
        [BatchItem(id=f"p{i}", datasource="observability", op="procedure", payload=item) for i in range(2)],
        mode="transactional",
    )
    assert batch.ok_count == 2


async def test_a_batch_item_outside_the_callers_datasources_is_refused(gateway: HttpGateway) -> None:
    """``batch`` admits the request; every item must still pass its own route's rule."""
    # The wire body, since ``HttpGateway.batch`` only names this caller's own datasources:
    # neondb_dev is a real datasource of the gateway that zonic-agent-runtime is not granted.
    item = {
        "id": "x",
        "datasource": "neondb_dev",
        "op": "view",
        "payload": {"name": "v_run", "schema": "agent_runtime", "limit": 1},
    }
    with pytest.raises(ZonicError) as info:
        await gateway._post(
            "/v1/batch", {"mode": "sequential", "items": [item]}, retry=False, request_id=None
        )
    assert info.value.details.get("gateway_status") == 403
