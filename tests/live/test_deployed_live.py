"""The deployed runtime through its load balancer (plan P5-03, P5-04: V-06; checkpoint M5).

Needs ``ZONIC_DEPLOYED_URL`` (the stack's ``Url`` output, ``deploy/aws/deploy.sh``) and the test
identity of ``docs/runbooks/test-client.md`` (``DEV_VERIFY_POOL_ID``, ``DEV_VERIFY_CLIENT_ID``,
``DEV_VERIFY_USERNAME``, ``DEV_VERIFY_PASSWORD`` in the process environment or ``.env``), signed in
with Cognito's admin password flow through the account's credentials (``AWS_PROFILE``). The runs
use the deployed runtime's real models.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

import httpx
import pytest
from dotenv import dotenv_values
from scripts.run_client import RuntimeClient

pytestmark = pytest.mark.live

_ENV_FILE = Path(__file__).resolve().parents[2] / ".env"


def setting(name: str) -> str | None:
    return os.environ.get(name) or dotenv_values(_ENV_FILE).get(name) or None


def access_token() -> str:
    import boto3

    names = ("DEV_VERIFY_POOL_ID", "DEV_VERIFY_CLIENT_ID", "DEV_VERIFY_USERNAME", "DEV_VERIFY_PASSWORD")
    values = [setting(n) for n in names]
    if not all(values):
        pytest.skip("the test identity (DEV_VERIFY_*) is not configured")
    pool, client, username, password = values
    answer = boto3.client("cognito-idp", region_name="us-east-1").admin_initiate_auth(
        UserPoolId=pool,
        ClientId=client,
        AuthFlow="ADMIN_USER_PASSWORD_AUTH",
        AuthParameters={"USERNAME": username, "PASSWORD": password},
    )
    return str(answer["AuthenticationResult"]["AccessToken"])


@pytest.fixture
async def deployed() -> Any:
    url = setting("ZONIC_DEPLOYED_URL")
    if not url:
        pytest.skip("ZONIC_DEPLOYED_URL is not set (deploy/aws/deploy.sh prints it)")
    token = access_token()
    async with httpx.AsyncClient(base_url=url, timeout=httpx.Timeout(120, connect=10)) as http:
        yield http, RuntimeClient(http, token)


async def test_V06_tokens_stream_incrementally_through_the_deployed_load_balancer(deployed: Any) -> None:
    http, client = deployed
    ready = (await http.get("/readyz")).json()
    assert ready["status"] in ("ok", "degraded"), ready
    for name, value in (("x-content-type-options", "nosniff"), ("x-frame-options", "DENY")):
        assert (await http.get("/healthz")).headers[name] == value
    catalog = {a["name"]: a["agenticWorkflowId"] for a in await client.catalog()}
    assert "fsh-facility-agent" in catalog, sorted(catalog)
    arrivals: list[float] = []

    def seen(event: dict[str, Any]) -> None:
        if event["type"] == "token.delta":
            arrivals.append(time.perf_counter())

    run = await client.run(
        "Which facilities are over budget?", workflow_id=catalog["fsh-facility-agent"], on_event=seen
    )
    assert run.final is not None and run.final["type"] == "run.completed", run.final
    assert len(arrivals) >= 5, "too few token frames to judge streaming"
    # Incremental: the frames spread over the answer, not one buffered burst at the end.
    assert arrivals[-1] - arrivals[0] > 0.5, arrivals[-1] - arrivals[0]
    roles = [m["role"] for m in await client.messages(run.session_id)]  # newest first
    assert roles[0] == "assistant" and roles[-1] == "user", roles


async def test_M5_a_dataset_the_run_names_is_paged_through_the_deployed_api(deployed: Any) -> None:
    _, client = deployed
    catalog = {a["name"]: a["agenticWorkflowId"] for a in await client.catalog()}
    run = await client.run(
        "Write a SQL query for the ten facilities with the highest energy cost, and run it.",
        workflow_id=catalog["fsh-facility-agent"],
    )
    assert run.final is not None and run.final["type"] == "run.completed", run.final
    if not run.dataset_ids:
        pytest.skip("this answer kept no dataset (the model took the analysis branch)")
    page = await client.dataset(run.dataset_ids[0], page=1, page_size=5)
    assert page["total"] >= len(page["rows"]) and page["page"] == 1


async def test_M5_an_approval_pause_is_answered_and_the_run_completes(deployed: Any) -> None:
    _, client = deployed
    catalog = {a["name"]: a["agenticWorkflowId"] for a in await client.catalog()}
    try:
        run = await client.run(
            "Electricity costs doubled at Baruch Houses in Q1 2024: write the report for publication.",
            workflow_id=catalog["facility-operations"],
        )
    except RuntimeError as exc:  # its Script node needs the sandbox (ZONIC_SANDBOX_URL)
        if "FEATURE_DISABLED" in str(exc):
            pytest.skip("facility-operations needs the Script node's sandbox, not deployed")
        raise
    if not run.waiting:
        pytest.skip(f"the run did not pause for approval ({run.final and run.final['type']})")
    [pending] = await client.interrupts(run.run_id)
    assert pending["kind"] == "approval"
    done = await client.resume(run, lambda _: {"approved": True, "comment": "M5 check"})
    assert done.final is not None and done.final["type"] == "run.completed", done.final
