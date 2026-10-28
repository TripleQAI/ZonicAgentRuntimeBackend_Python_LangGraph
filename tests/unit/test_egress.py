"""The egress guard and the HTTP node (P3-03: N-14, X-02, X-03). No request leaves the process:
the resolver is fixed and the transport is ``FakeUpstream``."""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import httpx
import pytest

from tests.fakes.egress import PUBLIC, FakeUpstream
from tests.kinds.harness import DEFAULT_INPUT, Harness
from zonic_runtime.config.env import EnvironmentModel
from zonic_runtime.config.settings import load_resources
from zonic_runtime.engine.kinds.core import core_kinds
from zonic_runtime.engine.node_template import ServicePorts
from zonic_runtime.errors import ZonicError
from zonic_runtime.ports.egress import EgressRequest

DEV = load_resources(EnvironmentModel(env="dev"))
URL = "https://api.example.test/items"


def ok(body: Any = None, status: int = 200, **headers: str) -> Any:
    def answer(_: httpx.Request) -> httpx.Response:
        if isinstance(body, (dict, list)):
            return httpx.Response(status, json=body, headers=headers)
        return httpx.Response(status, text=body or "", headers=headers)

    return answer


async def denied(guard: Any, url: str, **fields: Any) -> ZonicError:
    with pytest.raises(ZonicError) as caught:
        await guard.send(EgressRequest(method="GET", url=url))
    assert caught.value.code == "EGRESS_DENIED", caught.value
    for key, value in fields.items():
        assert caught.value.details.get(key) == value, caught.value.details
    return caught.value


# --- X-02 --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "address",
    [
        "169.254.169.254",
        "10.0.0.5",
        "172.16.3.1",
        "192.168.1.9",
        "127.0.0.1",
        "::1",
        "fe80::1",
        "::ffff:10.0.0.1",
        "0.0.0.0",
    ],
)
async def test_X02_an_allowed_host_resolving_to_a_metadata_private_or_loopback_address_is_denied(
    address: str,
) -> None:
    upstream = FakeUpstream()
    guard = upstream.guard({"api.example.test": [PUBLIC, address]})  # one bad address is enough
    await denied(guard, URL, reason="address", address=address)
    assert upstream.seen == []


@pytest.mark.parametrize(
    ("url", "reason"),
    [
        ("http://api.example.test/items", "scheme"),
        ("https://other.example.test/items", "host"),
        ("https://example.org/items", "host"),  # *.example.org does not match the bare domain
        ("https://169.254.169.254/latest/meta-data", "host"),
        ("https://api.example.test:8443/items", "port"),
        ("https://unknown.example.org/items", "unresolved"),
    ],
)
async def test_X02_schemes_hosts_and_ports_off_the_allow_list_are_denied(url: str, reason: str) -> None:
    upstream = FakeUpstream()
    await denied(upstream.guard(), url, reason=reason)
    assert upstream.seen == []


async def test_X02_the_connection_goes_to_the_checked_address_and_tls_still_names_the_host() -> None:
    upstream = FakeUpstream()
    upstream.route("api.example.test", "/items", ok("fine"))
    response = await upstream.guard().send(EgressRequest(method="GET", url=URL))
    [sent] = upstream.seen
    assert response.status == 200 and response.text() == "fine"
    assert sent.url.host == PUBLIC and sent.headers["host"] == "api.example.test"
    assert sent.extensions["sni_hostname"] == "api.example.test"


async def test_X02_a_redirect_to_a_blocked_host_is_denied_and_never_requested() -> None:
    upstream = FakeUpstream()
    upstream.route(
        "api.example.test", "/items", ok(status=302, location="https://169.254.169.254/latest/meta-data")
    )
    await denied(upstream.guard(follow_redirects=True), URL, reason="host")
    assert [r.url.path for r in upstream.seen] == ["/items"]
    upstream.route("api.example.test", "/items", ok(status=302, location="https://inner.example.org/x"))
    await denied(
        upstream.guard(
            {"api.example.test": [PUBLIC], "inner.example.org": ["10.1.2.3"]}, follow_redirects=True
        ),
        URL,
        reason="address",
    )


async def test_X02_without_follow_redirects_a_redirect_is_returned_not_followed() -> None:
    upstream = FakeUpstream()
    upstream.route(
        "api.example.test", "/items", ok(status=302, location="https://api.example.test/elsewhere")
    )
    response = await upstream.guard().send(EgressRequest(method="GET", url=URL))
    assert response.status == 302 and response.headers["location"].endswith("/elsewhere")
    assert len(upstream.seen) == 1


async def test_X02_an_allowed_redirect_is_checked_and_followed() -> None:
    upstream = FakeUpstream()
    upstream.route("api.example.test", "/items", ok(status=307, location="/moved"))
    upstream.route("api.example.test", "/moved", ok({"moved": True}))
    response = await upstream.guard(follow_redirects=True).send(EgressRequest(method="GET", url=URL))
    assert response.status == 200 and json.loads(response.content) == {"moved": True}
    assert response.url.endswith("/moved")


async def test_X02_a_response_above_max_response_bytes_is_denied() -> None:
    upstream = FakeUpstream()
    upstream.route("api.example.test", "/items", ok("x" * 2000))
    await denied(upstream.guard(max_response_bytes=1000), URL, reason="maxResponseBytes")


async def test_X02_environment_proxies_are_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid:3128")
    upstream = FakeUpstream()
    upstream.route("api.example.test", "/items", ok("direct"))
    guard = upstream.guard()
    assert (await guard.send(EgressRequest(method="GET", url=URL))).text() == "direct"
    assert guard._http()._trust_env is False


# --- the HTTP node: N-14, X-03 ---------------------------------------------------------------------


def document(**config: Any) -> dict[str, Any]:
    node = {"id": "h", "type": "http_request", "name": "h", "config": {"method": "GET", "url": URL, **config}}
    out = {
        "id": "out",
        "type": "output",
        "config": {
            "outputMapping": [
                {"target": "status", "value": "{{nodes.h.output.status}}"},
                {"target": "json", "value": "{{nodes.h.output.json}}"},
            ]
        },
    }
    return {
        "schemaVersion": "2.0",
        "nodes": [{"id": "start", "type": "start", "config": {}}, node, out],
        "edges": [{"source": "start", "target": "h"}, {"source": "h", "target": "out"}],
    }


def harness(upstream: FakeUpstream, **guard: Any) -> Harness:
    return Harness(DEV, core_kinds(), ports=ServicePorts(egress=upstream.guard(**guard)))


def inputs(**flow: Any) -> dict[str, Any]:
    return {**DEFAULT_INPUT, "flow": {"values": flow, "provenance": {}, "conflicts": []}}


async def test_N14_list_headers_body_and_the_answer_reach_the_output() -> None:
    upstream = FakeUpstream()
    upstream.route("api.example.test", "/items", ok({"items": [1, 2]}, **{"x-total": "2"}))
    doc = document(
        method="POST",
        headers=[{"key": "x-api", "value": "{{flow.token}}"}, {"key": "accept", "value": "application/json"}],
        body='{"q": "{{flow.q}}"}',
    )
    final, *_ = await harness(upstream).run(doc, inputs(token="t-1", q="boilers"))
    [sent] = upstream.seen
    assert (
        sent.method == "POST"
        and sent.headers["x-api"] == "t-1"
        and sent.headers["accept"] == "application/json"
    )
    assert json.loads(sent.content) == {"q": "boilers"}
    output = final["nodes"]["h"]["output"]
    assert (
        output["status"] == 200
        and output["json"] == {"items": [1, 2]}
        and output["headers"]["x-total"] == "2"
    )


async def test_N14_the_node_timeout_is_honored_and_is_a_timeout_error() -> None:
    upstream = FakeUpstream()

    async def slow(_: httpx.Request) -> httpx.Response:
        await asyncio.sleep(2)
        return httpx.Response(200)

    upstream.route("api.example.test", "/items", slow)
    started = time.monotonic()
    with pytest.raises(ZonicError) as caught:
        await harness(upstream).run(document(timeoutSeconds=0.2))
    assert caught.value.code == "TIMEOUT" and caught.value.details["seconds"] == pytest.approx(0.18)
    assert time.monotonic() - started < 3.5  # two attempts cut at 0.18 s, with the retry backoff and jitter
    assert len(upstream.seen) == 2


async def test_N14_a_blocked_host_is_egress_denied() -> None:
    upstream = FakeUpstream()
    with pytest.raises(ZonicError, match="EGRESS_DENIED"):
        await harness(upstream).run(document(url="https://intranet.corp.test/admin"))
    assert upstream.seen == []


@pytest.mark.parametrize(("method", "attempts"), [("GET", 2), ("POST", 1)])
async def test_N14_a_503_is_retried_for_get_only(method: str, attempts: int) -> None:
    upstream = FakeUpstream()
    upstream.route("api.example.test", "/items", ok("busy", status=503))
    with pytest.raises(ZonicError, match="UPSTREAM_5XX"):
        await harness(upstream).run(document(method=method))
    assert len(upstream.seen) == attempts


async def test_N14_a_4xx_answer_is_an_output_not_an_error() -> None:
    upstream = FakeUpstream()
    upstream.route("api.example.test", "/items", ok({"error": "nope"}, status=404))
    final, *_ = await harness(upstream).run(document())
    assert final["nodes"]["h"]["output"]["status"] == 404 and len(upstream.seen) == 1


async def test_X03_template_values_in_url_path_segments_and_query_are_encoded() -> None:
    upstream = FakeUpstream()
    upstream.route("api.example.test", "/items/a%2F..%2Fadmin%3Fx%3D1", ok({"ok": True}))
    doc = document(url="https://api.example.test/items/{{flow.id}}?q={{flow.q}}&page=2")
    await harness(upstream).run(doc, inputs(id="a/../admin?x=1", q="a&b=c"))
    [sent] = upstream.seen
    assert sent.url.raw_path == b"/items/a%2F..%2Fadmin%3Fx%3D1?q=a%26b%3Dc&page=2"


async def test_X03_a_template_that_is_the_base_url_is_used_as_it_is() -> None:
    upstream = FakeUpstream()
    upstream.route("api.example.test", "/v1/items/a%20b", ok({"ok": True}))
    doc = document(url="{{flow.base}}/v1/items/{{flow.id}}")
    await harness(upstream).run(doc, inputs(base="https://api.example.test", id="a b"))
    assert upstream.seen[0].url.raw_path == b"/v1/items/a%20b"
