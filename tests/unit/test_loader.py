"""``WorkflowLoader`` over the in-memory gateway (P1-04: D-02, D-03, A-01 data part, F-02).

Also the document cache by ``(agenticWorkflowId, updated_at)``, the catalog cache and the
revision hash.
"""

from __future__ import annotations

import copy
from dataclasses import replace
from typing import Any

import pytest

from tests.fakes.clock import FakeClock
from tests.fakes.in_memory_gateway import InMemoryGateway
from tests.workflow_fixtures import BUILDER, SYNTHETIC, read_json
from zonic_runtime.config.env import EnvironmentModel
from zonic_runtime.config.settings import RuntimeResources, load_resources
from zonic_runtime.errors import ZonicError
from zonic_runtime.integrations.gateway.repository import GatewayRepository
from zonic_runtime.workflow.loader.loader import WorkflowCaller, WorkflowLoader
from zonic_runtime.workflow.loader.revision import canonical_json, revision_hash

DEV = load_resources(EnvironmentModel(env="dev"))
ALICE = WorkflowCaller("alice")
BOB = WorkflowCaller("bob")
T0, T1 = "2026-10-25T04:49:28Z", "2026-10-26T09:00:00Z"
SIMPLE = read_json(SYNTHETIC / "fan-in-unequal.json")


def published(resources: RuntimeResources = DEV) -> RuntimeResources:
    """The resources with production catalog rules: only runtime channels are listed."""
    catalog = resources.settings.catalog.model_copy(update={"include_unpublished": False})
    return replace(resources, settings=resources.settings.model_copy(update={"catalog": catalog}))


def row(
    workflow_id: str, name: str, channel: str | None, *, activities: list[str] | None = None, at: str = T0
) -> dict[str, Any]:
    return {
        "agentic_workflow_id": workflow_id,
        "name": name,
        "updated_at": at,
        "publish_channels": [] if channel is None else [{"code": channel}],
        "security": {"activities": [{"refId": a, "code": a, "name": a} for a in activities]}
        if activities
        else {},
        "node_count": 3,
        "runtime_channel": channel,
    }


def export(workflow_id: str, document: dict[str, Any] = SIMPLE) -> dict[str, Any]:
    return {
        "agentic_workflow_id": workflow_id,
        "is_root": True,
        "export_json": document,
        "owner_sub": "owner",
    }


@pytest.fixture
def gateway() -> InMemoryGateway:
    fake = InMemoryGateway()
    fake.put_rows(
        "runtime",
        "v_agent_catalog",
        [
            row("wf-api", "b api", "api"),
            row("wf-two", "a two channels", "api"),
            row("wf-two", "a two channels", "qi-ui"),
            row("wf-other", "c other channel", "teams"),
            row("wf-draft", "d unpublished", None),
            row("wf-home", "e home only", "api", activities=["10100002"]),
            row("template-approval-flow", "f template", "api"),
        ],
    )
    fake.put_rows(
        "builder", "v_agent_export", [export(i) for i in ("wf-api", "wf-two", "wf-draft", "wf-home")]
    )
    fake.put_rows(
        "builder",
        "v_agent_summary",
        [{"agentic_workflow_id": i, "updated_at": T0} for i in ("wf-api", "wf-two", "wf-draft", "wf-home")],
    )
    return fake


def loader(
    gateway: InMemoryGateway, resources: RuntimeResources | None = None, clock: FakeClock | None = None
) -> WorkflowLoader:
    return WorkflowLoader.from_resources(
        resources or published(), GatewayRepository(gateway), monotonic=(clock or FakeClock()).monotonic
    )


def reads(gateway: InMemoryGateway, view: str) -> int:
    return sum(1 for c in gateway.calls if c.kind == "view" and c.name == view)


# --- A-01 (data part), F-02: visibility --------------------------------------------------


async def test_A01_catalog_lists_root_enabled_published_workflows_for_every_caller(
    gateway: InMemoryGateway,
) -> None:
    subject = loader(gateway)
    # Cognito is the only access control (decision 0004): the builder's security.activities
    # on wf-home does not hide it.
    expected = ["wf-two", "wf-api", "wf-home"]
    assert [r.agentic_workflow_id for r in await subject.catalog(ALICE)] == expected
    assert [r.agentic_workflow_id for r in await subject.catalog(BOB)] == expected


async def test_A01_development_settings_also_list_unpublished_workflows(gateway: InMemoryGateway) -> None:
    ids = [r.agentic_workflow_id for r in await loader(gateway, DEV).catalog(BOB)]
    assert "wf-draft" in ids and "wf-other" in ids and len(ids) == len(set(ids))


async def test_A01_unlisted_and_unknown_workflows_are_the_same_not_found(gateway: InMemoryGateway) -> None:
    subject = loader(gateway)
    for workflow_id, caller in [("wf-draft", ALICE), ("wf-other", BOB), ("nope", ALICE)]:
        with pytest.raises(ZonicError) as caught:
            await subject.load(workflow_id, caller)
        assert caught.value.code == "WORKFLOW_NOT_FOUND"
    assert (await subject.load("wf-home", BOB)).agentic_workflow_id == "wf-home"


async def test_F02_template_ids_never_appear_in_the_catalog(gateway: InMemoryGateway) -> None:
    subject = loader(gateway, DEV)
    assert not [r for r in await subject.catalog(ALICE) if r.agentic_workflow_id.startswith("template-")]
    with pytest.raises(ZonicError, match="WORKFLOW_NOT_FOUND"):
        await subject.load("template-approval-flow", ALICE)


# --- caches --------------------------------------------------------------------------------


async def test_document_cache_is_keyed_by_id_and_updated_at(gateway: InMemoryGateway) -> None:
    subject = loader(gateway)
    first = await subject.load("wf-api", ALICE)
    again = await subject.load("wf-api", ALICE)
    assert again is first
    assert (reads(gateway, "v_agent_catalog"), reads(gateway, "v_agent_export")) == (2, 1)

    changed = copy.deepcopy(SIMPLE)
    changed["description"] = "edited in the builder"
    for table, value in ((("runtime", "v_agent_catalog"), T1), (("builder", "v_agent_export"), changed)):
        for stored in gateway.tables[table]:
            if stored["agentic_workflow_id"] == "wf-api":
                stored["updated_at" if table[0] == "runtime" else "export_json"] = value
    edited = await subject.load("wf-api", ALICE)
    assert edited.revision != first.revision and reads(gateway, "v_agent_export") == 2


async def test_catalog_rows_are_cached_for_cache_seconds(gateway: InMemoryGateway) -> None:
    clock = FakeClock()
    subject = loader(gateway, clock=clock)
    await subject.catalog(ALICE)
    clock.advance(DEV.settings.catalog.cache_seconds - 1)
    await subject.catalog(BOB)
    assert reads(gateway, "v_agent_catalog") == 1
    clock.advance(1)
    await subject.catalog(BOB)
    assert reads(gateway, "v_agent_catalog") == 2


async def test_sub_flow_exports_load_with_one_in_read_and_share_the_cache(gateway: InMemoryGateway) -> None:
    subject = loader(gateway)
    loaded = await subject.load_exports(["wf-two", "wf-draft"])
    assert [d.agentic_workflow_id for d in loaded] == ["wf-two", "wf-draft"]
    assert reads(gateway, "v_agent_export") == 1
    await subject.load_exports(["wf-draft"])
    assert reads(gateway, "v_agent_export") == 1
    with pytest.raises(ZonicError, match="WORKFLOW_NOT_FOUND"):
        await subject.load_exports(["wf-two", "missing"])


# --- D-02: versions ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("version", "expected"), [(None, "1.0"), ("1.0", "1.0"), ("2.0", "2.0"), ("2.3", "2.3")]
)
def test_D02_accepted_versions(version: str | None, expected: str) -> None:
    document = copy.deepcopy(SIMPLE)
    document.pop("schemaVersion", None)
    if version is not None:
        document["schemaVersion"] = version
    assert WorkflowLoader.from_resources(DEV).from_stored(document).version == expected


def test_D02_an_unknown_major_version_is_unsupported() -> None:
    with pytest.raises(ZonicError) as caught:
        WorkflowLoader.from_resources(DEV).from_stored({**SIMPLE, "schemaVersion": "3.0"})
    assert caught.value.code == "DOCUMENT_VERSION_UNSUPPORTED"
    assert caught.value.details == {"version": "3.0", "accepted": ["1", "2"]}


def test_D02_a_version_that_is_not_a_string_is_invalid() -> None:
    with pytest.raises(ZonicError) as caught:
        WorkflowLoader.from_resources(DEV).from_stored({**SIMPLE, "schemaVersion": 2})
    assert caught.value.code == "DOCUMENT_INVALID"
    assert [p["path"] for p in caught.value.details["problems"]] == ["schemaVersion"]


# --- D-03: invalid documents -------------------------------------------------------------


def _problems(document: Any) -> list[str]:
    with pytest.raises(ZonicError) as caught:
        WorkflowLoader.from_resources(DEV).from_stored(document)
    assert caught.value.code == "DOCUMENT_INVALID"
    return [p["path"] for p in caught.value.details["problems"]]


def test_D03_document_errors_carry_paths() -> None:
    document = copy.deepcopy(SIMPLE)
    del document["nodes"][1]["id"]
    document["edges"][0]["source"] = 7
    assert _problems(document) == ["nodes[1].id", "edges[0].source"]


def test_D03_node_configuration_errors_carry_paths() -> None:
    document = copy.deepcopy(read_json(SYNTHETIC / "external-agent.json"))
    index, node = next((i, n) for i, n in enumerate(document["nodes"]) if "apiKey" in n.get("config", {}))
    node["config"]["apiKey"] = "sk-literal-secret"
    assert _problems(document) == [f"nodes[{index}].config"]


def test_D03_a_document_that_is_not_an_object_is_invalid() -> None:
    assert _problems([1, 2]) == [""]


# --- revision hash ------------------------------------------------------------------------


def test_revision_ignores_key_order_and_layout_but_not_content() -> None:
    subject = WorkflowLoader.from_resources(DEV)
    base = subject.from_stored(SIMPLE).revision
    reordered = {k: SIMPLE[k] for k in reversed(list(SIMPLE))}
    moved = copy.deepcopy(SIMPLE)
    moved["nodes"][0]["position"] = {"x": 999, "y": -1}
    edited = copy.deepcopy(SIMPLE)
    edited["nodes"][0].setdefault("config", {})["description"] = "changed"
    assert subject.from_stored(reordered).revision == base
    assert subject.from_stored(moved).revision == base
    assert subject.from_stored(edited).revision != base


def test_canonical_json_is_sorted_and_compact() -> None:
    assert canonical_json({"b": [{"z": 1, "a": "é"}], "a": None}) == '{"a":null,"b":[{"a":"é","z":1}]}'
    assert revision_hash({"a": 1}) == revision_hash({"a": 1}) != revision_hash({"a": 2})


def test_platform_documents_load_through_the_gateway_path(gateway: InMemoryGateway) -> None:
    stored = read_json(BUILDER / "platform" / "quick-pay-assistant.json")
    assert WorkflowLoader.from_resources(DEV).from_stored(stored).version == "1.0"
