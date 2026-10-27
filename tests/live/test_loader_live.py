"""The workflow loader and blueprint on the product database through the deployed gateway (P1-04, P1-05, opt-in).

Reads only: the catalog, the builder's summary and export views. Development settings list
unpublished root workflows, which is what the product database holds today.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import replace

import pytest

from tests.live.conftest import live_environment
from zonic_runtime.config.settings import load_resources
from zonic_runtime.errors import ZonicError
from zonic_runtime.integrations.gateway.http_gateway import HttpGateway
from zonic_runtime.integrations.gateway.repository import GatewayRepository
from zonic_runtime.workflow.blueprint.builder import BlueprintBuilder
from zonic_runtime.workflow.loader.loader import WorkflowCaller, WorkflowLoader

pytestmark = pytest.mark.live

ENV = live_environment()
RESOURCES = load_resources(ENV)


@pytest.fixture
async def loader() -> AsyncIterator[WorkflowLoader]:
    gateway = HttpGateway(RESOURCES.settings.gateway, ENV)
    try:
        yield WorkflowLoader.from_resources(RESOURCES, GatewayRepository(gateway))
    finally:
        await gateway.aclose()


async def test_live_catalog_rows_load_as_documents(loader: WorkflowLoader) -> None:
    caller = WorkflowCaller("live-test")
    rows = await loader.catalog(caller)
    assert rows, "the product database has no runnable root workflow"
    for row in rows:
        loaded = await loader.load(row.agentic_workflow_id, caller)
        assert loaded.document.nodes and len(loaded.revision) == 64
        assert (await loader.load(row.agentic_workflow_id, caller)) is loaded


async def test_live_sub_flow_exports_load_with_one_read(loader: WorkflowLoader) -> None:
    rows = await loader.catalog(WorkflowCaller("live-test"))
    ids = [r.agentic_workflow_id for r in rows]
    assert [d.agentic_workflow_id for d in await loader.load_exports(ids)] == ids


async def test_live_workflows_plan(loader: WorkflowLoader) -> None:
    caller = WorkflowCaller("live-test")
    # facility-operations has script nodes, which run only with the scriptNode feature on.
    features = RESOURCES.settings.features.model_copy(update={"script_node": True})
    builder = BlueprintBuilder(
        replace(RESOURCES, settings=RESOURCES.settings.model_copy(update={"features": features}))
    )
    for row in await loader.catalog(caller):
        loaded = await loader.load(row.agentic_workflow_id, caller)
        if row.name == "new-workflow":  # the builder's invalid example: a Swarm with no members
            with pytest.raises(ZonicError) as caught:
                builder.build(loaded)
            assert "SW-1" in {p["details"].get("rule_id") for p in caught.value.details["problems"]}
            continue
        blueprint = builder.build(loaded)
        assert not blueprint.errors and blueprint.nodes[blueprint.entry].compiled
