"""The integration hook types (P3-10: K-08, K-14): ``webhook``, ``cache``, ``mask``, ``guardrail``,
``rateLimit``, ``mcpTool`` and ``annotate``, the stream masking of masked nodes, hook observations."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import uuid
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from tests.conformance.test_kinds import llm as node
from tests.fakes.egress import PUBLIC, FakeUpstream
from tests.fakes.scripted_chat_model import Turn
from tests.kinds.agents import agent_harness
from tests.kinds.harness import EchoKind, Harness, Recorder, ResultKind
from tests.kinds.streaming import DriverHarness
from tests.unit.test_core_kinds import agent, output
from tests.unit.test_gateway_exporter import FakeIngest, exporter
from tests.unit.test_hooks import DEV, binding, chain
from zonic_runtime.config.settings import RuntimeResources
from zonic_runtime.engine.node_template import NodeServices
from zonic_runtime.errors import ZonicError
from zonic_runtime.hooks.builtin import builtin_hooks
from zonic_runtime.hooks.integration import HookPorts, integration_hooks
from zonic_runtime.hooks.node_hooks import TemplateHooks
from zonic_runtime.hooks.registry import HookRegistry
from zonic_runtime.hooks.resolver import HookResolver
from zonic_runtime.hooks.runner import HookRunner
from zonic_runtime.integrations.langfuse.provider import LangfuseTracer
from zonic_runtime.models.hooks.binding import HookBindings
from zonic_runtime.ports.tracer import NoopTrace

EMAIL = "jane.doe@example.com"
ENDPOINT = "https://hooks.example.org/zonic"
SECRET = "s3cret-signing-key"


def resources(**hooks: Any) -> RuntimeResources:
    settings = DEV.settings.model_copy(update={"hooks": DEV.settings.hooks.model_copy(update=hooks)})
    return replace(DEV, settings=settings)


def hooks_with(bindings: list[dict[str, Any]], base: RuntimeResources = DEV, **ports: Any) -> TemplateHooks:
    services = NodeServices.from_resources(base)
    hook_ports = HookPorts(hooks=base.settings.hooks, guardrails=base.settings.guardrails, **ports)
    registry = HookRegistry([*builtin_hooks(services.schemas), *integration_hooks(hook_ports)])
    parsed = HookBindings.model_validate({"bindings": bindings})
    resolver = HookResolver(registry, parsed, base.settings.hooks)
    return TemplateHooks(HookRunner(resolver, base.settings.hooks), services)


def echo_harness(
    hooks: TemplateHooks, base: RuntimeResources = DEV, text: str | None = None, **fields: Any
) -> Harness:
    """``llm`` nodes that answer ``text`` with ``fields`` (spread into the open output)."""
    recorder = Recorder()
    raw = {"text": text, "stopReason": "stop", "fields": fields} if text is not None else None
    factories = {"llm": lambda s, b: EchoKind(s, b, recorder, raw=raw)}
    return Harness(base, factories, recorder=recorder, hooks=hooks)


def turn(query: str) -> dict[str, Any]:
    return {"system": {"values": {"userQuery": query}, "provenance": {}, "conflicts": []}}


def in_session(h: Harness, session_id: str) -> Harness:
    """The harness's runs in another session (the default one is ``s-1``)."""
    base = h.context

    def context(blueprint: Any, run_id: str = "run-1") -> Any:
        ctx = base(blueprint, run_id)
        return replace(ctx, run=replace(ctx.run, session_id=session_id))

    h.context = context  # type: ignore[method-assign]
    return h


def warnings_of(events: list[dict[str, Any]]) -> list[str]:
    return [e.get("code", "") for e in events if e.get("event") == "warning"]


# --- K-08: webhook ------------------------------------------------------------------------------


class GatedUpstream(FakeUpstream):
    """Answers the webhook only once ``release`` is set, so a waiting run would hang."""

    def __init__(self) -> None:
        super().__init__()
        self.release = asyncio.Event()

        async def answer(request: httpx.Request) -> httpx.Response:
            await self.release.wait()
            return httpx.Response(204)

        self.route("hooks.example.org", "/zonic", answer)


async def test_K08_an_asynchronous_webhook_does_not_delay_the_run_and_its_payload_is_masked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ZONIC_SECRET_HOOK_KEY", SECRET)
    from zonic_runtime.integrations.secrets.environment import EnvironmentSecrets

    upstream = GatedUpstream()
    guard = upstream.guard({"hooks.example.org": [PUBLIC]})
    base = resources(endpoints={"audit": ENDPOINT})
    hooks = hooks_with(
        [
            binding(
                "audit",
                "afterNode",
                hook="webhook",
                match={"nodeNames": ["a"]},
                config={"endpointRef": "audit", "secretRef": "{{secrets.HOOK_KEY}}", "maskKeys": ["token"]},
            )
        ],
        base,
        egress=guard,
        secrets=EnvironmentSecrets(),
    )
    h = echo_harness(hooks, base, text=f"Mail {EMAIL}", token="abc123")
    final, *_ = await asyncio.wait_for(h.run(chain(node("a"))), timeout=5)
    assert final["nodes"]["a"]["output"]["text"] == f"Mail {EMAIL}"  # the run itself is untouched
    upstream.release.set()
    await hooks.runner.idle()
    [sent] = upstream.seen
    body = sent.content.decode()
    payload = json.loads(body)
    assert payload["bindingId"] == "audit" and payload["point"] == "afterNode" and payload["nodeId"] == "a"
    assert payload["output"]["text"] == "Mail [REDACTED]" and payload["output"]["token"] == "[REDACTED]"
    assert EMAIL not in body
    expected = hmac.new(SECRET.encode(), sent.content, hashlib.sha256).hexdigest()
    assert sent.headers["x-zonic-signature"] == f"sha256={expected}"
    assert sent.headers["x-zonic-hook"] == "audit"
    await guard.aclose()


async def test_K08_a_blocking_webhook_can_veto_the_node() -> None:
    upstream = FakeUpstream()
    upstream.route(
        "hooks.example.org",
        "/zonic",
        lambda r: httpx.Response(200, json={"action": "fail", "code": "INPUT_INVALID"}),
    )
    guard = upstream.guard({"hooks.example.org": [PUBLIC]})
    base = resources(endpoints={"audit": ENDPOINT})
    config = {"endpointRef": "audit", "mode": "blocking"}
    hooks = hooks_with(
        [binding("audit", "beforeNode", hook="webhook", match={"nodeNames": ["a"]}, config=config)],
        base,
        egress=guard,
    )
    h = echo_harness(hooks, base)
    with pytest.raises(ZonicError, match="INPUT_INVALID"):
        await h.run(chain(node("a")))
    assert h.recorder.calls == []
    await guard.aclose()


@pytest.mark.parametrize(
    ("endpoints", "reason"),
    [
        ({"audit": "https://elsewhere.test/zonic"}, "not on the egress allow list"),
        ({}, "unknown endpoint audit"),
    ],
    ids=["not-allow-listed", "unknown-endpoint"],
)
def test_K08_an_endpoint_off_the_allow_list_is_rejected_at_load(
    endpoints: dict[str, str], reason: str
) -> None:
    guard = FakeUpstream().guard({"hooks.example.org": [PUBLIC]})
    base = resources(endpoints=endpoints)
    with pytest.raises(ZonicError) as caught:
        hooks_with(
            [binding("audit", "afterNode", hook="webhook", config={"endpointRef": "audit"})],
            base,
            egress=guard,
        )
    assert caught.value.code == "RESOURCE_INVALID"
    assert any(reason in i["message"] for i in caught.value.details["issues"])


def test_K08_the_allow_list_check_needs_no_request() -> None:
    guard = FakeUpstream().guard()
    assert guard.allows("https://api.example.test/x") and guard.allows("https://a.example.org/")
    assert not guard.allows("http://api.example.test/x")  # scheme
    assert not guard.allows("https://api.example.test:8443/x")  # port
    assert not guard.allows("https://evil.test/") and not guard.allows("not a url")


# --- K-14: cache --------------------------------------------------------------------------------


def cache_bindings(**config: Any) -> list[dict[str, Any]]:
    settings = {"ttlSeconds": 60, **config}
    return [
        binding(
            "memo", ["beforeNode", "afterNode"], hook="cache", match={"nodeNames": ["a"]}, config=settings
        )
    ]


async def test_K14_cache_hits_in_the_same_session_and_misses_in_another() -> None:
    hooks = hooks_with(cache_bindings())
    h = echo_harness(hooks)
    first, *_ = await h.run(chain(node("a")), run_id="run-1")
    second, *_ = await h.run(chain(node("a")), run_id="run-2")
    assert h.recorder.calls == ["a"]  # the second run skipped the node
    assert second["nodes"]["a"]["output"] == first["nodes"]["a"]["output"]
    other = in_session(echo_harness(hooks), "s-2")
    await other.run(chain(node("a")), run_id="run-3")
    assert other.recorder.calls == ["a"]


async def test_K14_a_user_scoped_cache_is_shared_across_sessions_and_keys_resolve() -> None:
    hooks = hooks_with(cache_bindings(scope="user", key="{{system.userQuery}}"))
    h = echo_harness(hooks)
    await h.run(chain(node("a")), run_id="run-1")
    later = in_session(echo_harness(hooks), "s-9")
    await later.run(chain(node("a")), run_id="run-2")
    assert h.recorder.calls == ["a"] and later.recorder.calls == []


async def test_K14_a_cached_entry_expires_after_its_ttl() -> None:
    now = [1000.0]
    from zonic_runtime.hooks.integration import CacheHook

    hook = CacheHook(DEV.settings.hooks, now=lambda: now[0])
    services = NodeServices.from_resources(DEV)
    registry = HookRegistry([hook])
    parsed = HookBindings.model_validate({"bindings": cache_bindings(ttlSeconds=5)})
    hooks = TemplateHooks(
        HookRunner(HookResolver(registry, parsed, DEV.settings.hooks), DEV.settings.hooks), services
    )
    h = echo_harness(hooks)
    await h.run(chain(node("a")), run_id="run-1")
    await h.run(chain(node("a")), run_id="run-2")
    now[0] += 6
    await h.run(chain(node("a")), run_id="run-3")
    assert h.recorder.calls == ["a", "a"]


# --- K-14: mask ---------------------------------------------------------------------------------


async def test_K14_mask_replaces_keys_and_patterns_in_state() -> None:
    config = {"keys": ["token"], "patterns": ["email", "/\\bACME-\\d+\\b/"]}
    hooks = hooks_with([binding("pii", "afterNode", hook="mask", match={"nodeNames": ["a"]}, config=config)])
    h = echo_harness(hooks, text=f"Write {EMAIL} about ACME-42", token="abc", nested={"token": "x"})
    final, events, *_ = await h.run(chain(node("a")))
    out = final["nodes"]["a"]["output"]
    assert out["text"] == "Write [REDACTED] about [REDACTED]"
    assert out["token"] == "[REDACTED]" and out["nested"] == {"token": "[REDACTED]"}
    assert EMAIL not in json.dumps(events, default=str)


def test_K14_unknown_mask_patterns_are_rejected_at_load() -> None:
    with pytest.raises(ZonicError) as caught:
        hooks_with([binding("pii", "afterNode", hook="mask", config={"patterns": ["nope"]})])
    assert caught.value.code == "RESOURCE_INVALID" and "unknown patterns nope" in str(caught.value.details)


async def test_K14_a_masked_agent_streams_masked_text_and_traces_its_masked_output() -> None:
    reply = f"Sure, I will email {EMAIL} today."
    h, _ = agent_harness([Turn.say(reply)], DEV)
    h.hooks = hooks_with(
        [
            binding(
                "pii", "afterNode", hook="mask", match={"nodeNames": ["ag"]}, config={"patterns": ["email"]}
            )
        ]
    )
    ingest = FakeIngest()
    lf = LangfuseTracer(DEV.settings.observability, DEV.observability, exporter(ingest), release="r1")
    result = await DriverHarness(h, tracer=lf).run(chain(agent(), output()), run_id=str(uuid.uuid4()))
    lf.flush()
    lf.shutdown()
    assert result.outcome.status == "succeeded"
    assert result.text("ag") == "Sure, I will email [REDACTED] today."  # the stream, whole
    assert result.outcome.state is not None
    assert result.outcome.state["nodes"]["ag"]["output"]["text"] == "Sure, I will email [REDACTED] today."
    finished = [e for e in result.events if e["type"] == "node.finished" and e.get("nodeId") == "ag"]
    assert finished and EMAIL not in json.dumps(finished)
    [node_span] = [
        o for o in ingest.observations.values() if o.get("nodeId") == "ag" and o["type"] == "agent"
    ]
    assert EMAIL not in json.dumps(node_span.get("output"))
    spans = [o for o in ingest.observations.values() if o["name"] == "hook:pii"]
    assert spans and spans[0]["parentObservationId"] == node_span["observationId"]  # hook observation


async def test_K14_the_stream_masker_holds_back_a_match_split_across_deltas() -> None:
    from zonic_runtime.runtime.redact import StreamMasker

    masker = StreamMasker(["email"], DEV.settings.hooks.patterns, "[REDACTED]", holdback=32)
    pieces = ["Reach me at jane", ".doe@exa", "mple.com", " or later, thanks!"]
    streamed = "".join(masker.push(p) for p in pieces) + masker.flush()
    assert streamed == "Reach me at [REDACTED] or later, thanks!"


# --- K-14: guardrail ----------------------------------------------------------------------------


def guardrail(action: str, point: str = "beforeNode", **config: Any) -> list[dict[str, Any]]:
    settings = {"provider": "patterns", "patterns": ["email"], "action": action, **config}
    return [binding("safe", point, hook="guardrail", match={"nodeNames": ["a"]}, config=settings)]


async def test_K14_a_guardrail_blocks_a_flagged_input() -> None:
    h = echo_harness(hooks_with(guardrail("block", fields=["input.prompt"])))
    with pytest.raises(ZonicError, match="GUARDRAIL_BLOCKED"):
        await h.run(chain(node("a")), inputs=turn(f"write to {EMAIL}"))
    assert h.recorder.calls == []


async def test_K14_a_guardrail_warns_and_redacts_the_output() -> None:
    h = echo_harness(hooks_with(guardrail("warn", "afterNode")), text=f"to {EMAIL}")
    final, events, *_ = await h.run(chain(node("a")))
    assert final["nodes"]["a"]["output"]["text"] == f"to {EMAIL}"
    assert "guardrail.flagged" in warnings_of(events)
    h = echo_harness(hooks_with(guardrail("redact", "afterNode", fields=["output.text"])), text=f"to {EMAIL}")
    final, *_ = await h.run(chain(node("a")))
    assert final["nodes"]["a"]["output"]["text"] == "to [REDACTED]"


async def test_K14_a_clean_value_passes_the_guardrail() -> None:
    h = echo_harness(hooks_with(guardrail("block", "afterNode")), text="nothing personal")
    final, *_ = await h.run(chain(node("a")))
    assert h.recorder.calls == ["a"] and final["nodes"]["a"]["output"]["text"] == "nothing personal"


@pytest.mark.parametrize(
    ("config", "reason"),
    [
        ({"provider": "policy_model", "policyRef": "default"}, "not configured"),
        ({"provider": "bedrock", "guardrailId": "g1"}, "bedrock guardrail provider is not configured"),
        ({"provider": "patterns", "patterns": []}, "needs patterns"),
    ],
    ids=["policy-model", "bedrock", "no-patterns"],
)
def test_K14_a_guardrail_without_its_provider_is_rejected_at_load(
    config: dict[str, Any], reason: str
) -> None:
    with pytest.raises(ZonicError) as caught:
        hooks_with([binding("safe", "beforeNode", hook="guardrail", config=config)])
    assert caught.value.code == "RESOURCE_INVALID" and reason in str(caught.value.details)


# --- K-14: beforeRun and afterRun ---------------------------------------------------------------


def result_harness(hooks: TemplateHooks) -> DriverHarness:
    recorder = Recorder()
    return DriverHarness(
        Harness(DEV, {"output": lambda s, b: ResultKind(s, b, recorder)}, recorder=recorder, hooks=hooks)
    )


async def test_K14_before_run_guardrail_rejects_the_users_message() -> None:
    config = {"provider": "patterns", "patterns": ["email"], "action": "block"}
    driver = result_harness(hooks_with([binding("gate", "beforeRun", hook="guardrail", config=config)]))
    run, graph = driver.prepare(chain({"id": "out", "type": "output", "config": {}}))
    found = await driver.drive(run, graph, turn(f"write to {EMAIL}"))
    assert found.outcome.status == "failed" and found.outcome.error is not None
    assert found.outcome.error.code == "GUARDRAIL_BLOCKED"


async def test_K14_after_run_mask_transforms_the_result() -> None:
    bindings = [
        binding(
            "shape",
            "afterRun",
            hook="transform",
            config={"set": [{"path": "result.contact", "value": EMAIL}]},
            order=1,
        ),
        binding("pii", "afterRun", hook="mask", config={"patterns": ["email"]}, order=2),
    ]
    run = await result_harness(hooks_with(bindings)).run(chain({"id": "out", "type": "output", "config": {}}))
    assert run.outcome.result == {"answer": "out", "contact": "[REDACTED]"}


# --- rateLimit, mcpTool, annotate ---------------------------------------------------------------


async def test_rate_limit_fails_the_execution_over_the_limit_per_scope() -> None:
    config = {"limit": 1, "windowSeconds": 60, "scope": "session"}
    hooks = hooks_with(
        [binding("rl", "beforeNode", hook="rateLimit", match={"nodeNames": ["a"]}, config=config)]
    )
    h = echo_harness(hooks)
    await h.run(chain(node("a")), run_id="run-1")
    with pytest.raises(ZonicError, match="RATE_LIMITED"):
        await h.run(chain(node("a")), run_id="run-2")
    other = in_session(echo_harness(hooks), "s-2")
    await other.run(chain(node("a")), run_id="run-3")
    assert other.recorder.calls == ["a"]


class FakeMcp:
    def __init__(self, answer: Any, is_error: bool = False) -> None:
        self.answer, self.is_error = answer, is_error
        self.calls: list[tuple[str | None, str, dict[str, Any]]] = []

    async def call(self, server_id: str | None, tool: str, arguments: Any) -> Any:
        self.calls.append((server_id, tool, dict(arguments)))
        text = json.dumps(self.answer)
        return SimpleNamespace(
            structured_content=None, content=[SimpleNamespace(type="text", text=text)], is_error=self.is_error
        )


async def test_mcp_tool_sets_its_result_in_the_output() -> None:
    mcp = FakeMcp({"score": {"value": 0.92}})
    config = {
        "server": "stats",
        "tool": "score",
        "inputMapping": [{"key": "text", "value": "{{nodeOutput.text}}"}],
        "outputMapping": [{"target": "output.score", "path": "score.value"}],
    }
    hooks = hooks_with(
        [binding("rank", "afterNode", hook="mcpTool", match={"nodeNames": ["a"]}, config=config)], mcp=mcp
    )
    h = echo_harness(hooks, text="hello")
    final, *_ = await h.run(chain(node("a")))
    assert mcp.calls == [("stats", "score", {"text": "hello"})]
    assert final["nodes"]["a"]["output"]["score"] == 0.92


async def test_mcp_tool_errors_fail_with_tool_failed_and_need_a_server_at_load() -> None:
    config = {"server": "stats", "tool": "score", "outputMapping": [{"target": "output.score"}]}
    bindings = [binding("rank", "afterNode", hook="mcpTool", match={"nodeNames": ["a"]}, config=config)]
    h = echo_harness(hooks_with(bindings, mcp=FakeMcp("boom", is_error=True)))
    with pytest.raises(ZonicError, match="TOOL_FAILED"):
        await h.run(chain(node("a")))
    with pytest.raises(ZonicError) as caught:
        hooks_with(bindings)
    assert "MCP server is not configured" in str(caught.value.details)


class RecordingTrace(NoopTrace):
    def __init__(self) -> None:
        self.annotations: list[tuple[Any, list[str], dict[str, Any], Any]] = []
        self.spans: list[str] = []

    def span(self, config: Any, name: str, level: Any = "DEFAULT", metadata: Any = None) -> None:
        self.spans.append(name)

    def annotate(self, config: Any, tags: Any = (), metadata: Any = None, score: Any = None) -> None:
        self.annotations.append((config is None, list(tags), dict(metadata or {}), score))


async def test_annotate_tags_the_node_and_scores_the_run() -> None:
    bindings = [
        binding(
            "tagged",
            "afterNode",
            hook="annotate",
            match={"nodeNames": ["a"]},
            config={
                "tags": ["{{nodeOutput.text}}"],
                "metadata": {"length": 5},
                "score": {"name": "ok", "value": True},
            },
        ),
        binding("graded", "afterRun", hook="annotate", config={"score": {"name": "grade", "value": "A"}}),
    ]
    trace = RecordingTrace()
    recorder = Recorder()
    factories = {"llm": lambda s, b: EchoKind(s, b, recorder, raw={"text": "fine"})}
    h = Harness(
        DEV,
        {**factories, "output": lambda s, b: ResultKind(s, b, recorder)},
        recorder=recorder,
        hooks=hooks_with(bindings),
    )
    driver = DriverHarness(h)
    run, graph = driver.prepare(chain(node("a"), {"id": "out", "type": "output", "config": {}}))
    run = replace(run, context=replace(run.context, tracer=trace))
    found = await driver.drive(run, graph)
    assert found.outcome.status == "succeeded"
    assert trace.annotations == [
        (False, ["fine"], {"length": 5}, {"name": "ok", "value": True}),
        (True, [], {}, {"name": "grade", "value": "A"}),
    ]
    assert "hook:tagged" in trace.spans


async def test_annotate_scores_reach_the_gateway_ingest() -> None:
    config = {"tags": ["checked"], "score": {"name": "relevance", "value": 0.8, "comment": "auto"}}
    h, _ = agent_harness([Turn.say("Sunny.")], DEV)
    h.hooks = hooks_with(
        [binding("score", "afterNode", hook="annotate", match={"nodeNames": ["ag"]}, config=config)]
    )
    ingest = FakeIngest()
    lf = LangfuseTracer(DEV.settings.observability, DEV.observability, exporter(ingest), release="r1")
    result = await DriverHarness(h, tracer=lf).run(chain(agent(), output()), run_id=str(uuid.uuid4()))
    lf.flush()
    lf.shutdown()
    assert result.outcome.status == "succeeded"
    scores = [s for call in ingest.calls for s in call.scores]
    [score] = scores
    [node_span] = [
        o for o in ingest.observations.values() if o.get("nodeId") == "ag" and o["type"] == "agent"
    ]
    assert score["name"] == "relevance" and score["value"] == 0.8 and score["dataType"] == "NUMERIC"
    assert score["observationId"] == node_span["observationId"] and score["comment"] == "auto"
    assert node_span["metadata"]["zonic_annotation"] == {"tags": ["checked"]}
