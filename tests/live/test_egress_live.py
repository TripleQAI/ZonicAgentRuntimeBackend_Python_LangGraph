"""The egress guard on the real network (opt-in: ``uv run pytest -m live``; P3-03).

Real DNS and TLS: the connection goes to the checked address while the certificate is verified
against the host, and a redirect off the allow list is refused. Uses the dev allow list
(``httpbin.org``).
"""

from __future__ import annotations

import json

import pytest

from tests.live.conftest import live_environment
from zonic_runtime.config.settings import load_resources
from zonic_runtime.errors import ZonicError
from zonic_runtime.integrations.egress.guard import EgressGuard
from zonic_runtime.ports.egress import EgressRequest

pytestmark = pytest.mark.live

POLICY = load_resources(live_environment()).egress


async def test_live_a_pinned_https_call_verifies_the_hosts_certificate() -> None:
    guard = EgressGuard(POLICY)
    try:
        response = await guard.send(
            EgressRequest(method="GET", url="https://httpbin.org/get?x=1", headers=(("x-probe", "zonic"),))
        )
    finally:
        await guard.aclose()
    body = json.loads(response.content)
    assert response.status == 200 and body["args"] == {"x": "1"} and body["headers"]["X-Probe"] == "zonic"
    assert body["headers"]["Host"] == "httpbin.org"


async def test_live_a_redirect_off_the_allow_list_is_refused() -> None:
    guard = EgressGuard(POLICY.model_copy(update={"follow_redirects": True}))
    try:
        with pytest.raises(ZonicError) as caught:
            await guard.send(
                EgressRequest(
                    method="GET", url="https://httpbin.org/redirect-to?url=https%3A%2F%2Fexample.com%2F"
                )
            )
    finally:
        await guard.aclose()
    assert caught.value.code == "EGRESS_DENIED" and caught.value.details["host"] == "example.com"
