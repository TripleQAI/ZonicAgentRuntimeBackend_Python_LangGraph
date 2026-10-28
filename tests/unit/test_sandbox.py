"""The Script node and the Piston sandbox (P3-07: N-17).

The wrapper is executed for real with the local ``node`` and ``python`` (the programs Piston
would run); the Piston API itself is a fake transport, since no sandbox service is configured.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from dataclasses import replace
from typing import Any

import httpx
import pytest

from tests.kinds.harness import DEFAULT_INPUT, Harness
from zonic_runtime.config.env import EnvironmentModel
from zonic_runtime.config.settings import RuntimeResources, load_resources
from zonic_runtime.engine.kinds.core import core_kinds
from zonic_runtime.engine.node_template import ServicePorts
from zonic_runtime.errors import ZonicError
from zonic_runtime.integrations.sandbox.piston import RESULT_MARKER, PistonSandbox, script_result, wrap_script
from zonic_runtime.ports.sandbox import SandboxRequest

DEV = load_resources(EnvironmentModel(env="dev"))
SETTINGS = DEV.settings.sandbox
NODE = shutil.which("node")


def scripts_on(resources: RuntimeResources = DEV) -> RuntimeResources:
    features = resources.settings.features.model_copy(update={"script_node": True})
    return replace(resources, settings=resources.settings.model_copy(update={"features": features}))


def run_locally(language: str, program: str) -> str:
    command = [NODE or "node", "-e", program] if language == "javascript" else [sys.executable, "-c", program]
    return subprocess.run(command, capture_output=True, text=True, timeout=30, check=True).stdout


class FakePiston:
    """Runs the program it is sent with the local interpreter, as Piston would."""

    def __init__(self, signal: str | None = None, status: int = 200) -> None:
        self.bodies: list[dict[str, Any]] = []
        self.signal = signal
        self.status = status

    def handle(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.bodies.append(body)
        if self.status != 200:
            return httpx.Response(self.status, text="piston is down")
        if self.signal:
            return httpx.Response(
                200, json={"run": {"stdout": "", "stderr": "", "code": None, "signal": self.signal}}
            )
        try:
            stdout = run_locally(body["language"], body["files"][0]["content"])
            run = {"stdout": stdout, "stderr": "", "code": 0, "signal": None}
        except subprocess.CalledProcessError as error:
            run = {"stdout": error.stdout, "stderr": error.stderr, "code": error.returncode, "signal": None}
        return httpx.Response(200, json={"language": body["language"], "version": "x", "run": run})

    def sandbox(self) -> PistonSandbox:
        return PistonSandbox(SETTINGS, "https://piston.test/", transport=httpx.MockTransport(self.handle))


def document(code: str, inputs: Any, language: str | None = None) -> dict[str, Any]:
    config: dict[str, Any] = {"code": code, "inputs": inputs}
    if language:
        config["language"] = language
    out = {
        "id": "out",
        "type": "output",
        "config": {"outputMapping": [{"target": "severity", "value": "{{nodes.s.output.severity}}"}]},
    }
    return {
        "schemaVersion": "2.0",
        "nodes": [
            {"id": "start", "type": "start", "config": {}},
            {"id": "s", "type": "script", "name": "s", "config": config},
            out,
        ],
        "edges": [{"source": "start", "target": "s"}, {"source": "s", "target": "out"}],
    }


SEVERITY = """const match = /SEVERITY:\\s*(\\d+)/i.exec(String(input.report || ''));
const severity = match ? Number(match[1]) : 10;
return { severity, needsReview: severity >= Number(input.threshold ?? 7) };"""


def flow(**values: Any) -> dict[str, Any]:
    return {**DEFAULT_INPUT, "flow": {"values": values, "provenance": {}, "conflicts": []}}


# --- the wrapper, run for real ---------------------------------------------------------------------


@pytest.mark.skipif(NODE is None, reason="no node on PATH")
def test_N17_the_javascript_wrapper_returns_the_scripts_value_and_keeps_inputs_out_of_the_code() -> None:
    hostile = {"report": "'); process.exit(3); ('", "threshold": 2, "text": "SEVERITY: 9"}
    stdout = run_locally(
        "javascript",
        wrap_script("javascript", "return { seen: input.report, n: input.threshold * 2 };", hostile),
    )
    found, result, _ = script_result(stdout)
    assert found and result == {"seen": hostile["report"], "n": 4}


def test_N17_the_python_wrapper_returns_the_scripts_value() -> None:
    code = "total = sum(input['values'])\nreturn {'total': total, 'quote': input['q']}"
    stdout = run_locally("python", wrap_script("python", code, {"values": [1, 2, 3], "q": '"""; import os'}))
    found, result, _ = script_result(stdout)
    assert found and result == {"total": 6, "quote": '"""; import os'}


def test_N17_the_result_is_read_after_the_last_marker() -> None:
    found, result, before = script_result(f'log line\n{RESULT_MARKER}{{"a": 1}}\n')
    assert found and result == {"a": 1} and before == "log line"
    assert script_result("no marker") == (False, None, "no marker")


# --- the node ---------------------------------------------------------------------------------------


async def test_N17_with_the_feature_off_the_script_node_is_refused_at_planning() -> None:
    h = Harness(DEV, core_kinds(), ports=ServicePorts(sandbox=FakePiston().sandbox()))
    with pytest.raises(ZonicError) as caught:
        h.plan(document(SEVERITY, []))
    [problem] = caught.value.details["problems"]
    assert caught.value.code == "FEATURE_DISABLED" and problem["details"]["feature"] == "scriptNode"


async def test_N17_with_the_feature_on_and_no_sandbox_service_the_node_is_feature_disabled() -> None:
    with pytest.raises(ZonicError, match="FEATURE_DISABLED"):
        await Harness(scripts_on(), core_kinds()).run(document(SEVERITY, []))


@pytest.mark.skipif(NODE is None, reason="no node on PATH")
async def test_N17_with_the_sandbox_the_output_is_parsed_and_the_timeout_is_sent() -> None:
    piston = FakePiston()
    h = Harness(scripts_on(), core_kinds(), ports=ServicePorts(sandbox=piston.sandbox()))
    inputs = [
        {"key": "report", "value": "{{flow.report}}"},
        {"key": "threshold", "value": "5", "type": "number"},
    ]
    final, *_ = await h.run(document(SEVERITY, inputs), flow(report="All good. SEVERITY: 6"))
    assert final["nodes"]["s"]["output"] == {"severity": 6, "needsReview": True}
    [body] = piston.bodies
    assert body["language"] == "javascript" and body["version"] == "*"
    assert body["run_timeout"] == min(
        int(DEV.manifest.defaults.timeout_seconds * 1000), SETTINGS.run_timeout_milliseconds
    )


async def test_N17_a_script_killed_at_its_timeout_is_a_sandbox_error() -> None:
    h = Harness(
        scripts_on(), core_kinds(), ports=ServicePorts(sandbox=FakePiston(signal="SIGKILL").sandbox())
    )
    with pytest.raises(ZonicError) as caught:
        await h.run(document("while (true) {}", []))
    assert (
        caught.value.code == "SANDBOX_ERROR"
        and caught.value.details["timeout_ms"] == SETTINGS.run_timeout_milliseconds
    )


@pytest.mark.skipif(NODE is None, reason="no node on PATH")
async def test_N17_a_throwing_script_and_an_unknown_language_are_sandbox_errors() -> None:
    sandbox = FakePiston().sandbox()
    with pytest.raises(ZonicError) as thrown:
        await sandbox.run(SandboxRequest(language="javascript", code="throw new Error('boom');"))
    assert thrown.value.code == "SANDBOX_ERROR" and "boom" in thrown.value.details["reason"]
    with pytest.raises(ZonicError) as unknown:
        await sandbox.run(SandboxRequest(language="cobol", code="DISPLAY 'x'."))
    assert "cobol" in unknown.value.details["reason"]


async def test_N17_an_unreachable_or_failing_sandbox_service_is_a_sandbox_error() -> None:
    with pytest.raises(ZonicError) as caught:
        await FakePiston(status=503).sandbox().run(SandboxRequest(language="javascript", code="return 1;"))
    assert caught.value.code == "SANDBOX_ERROR" and caught.value.details["status"] == 503


@pytest.mark.skipif(NODE is None, reason="no node on PATH")
async def test_N17_a_value_that_is_not_an_object_is_the_output_value() -> None:
    h = Harness(scripts_on(), core_kinds(), ports=ServicePorts(sandbox=FakePiston().sandbox()))
    final, *_ = await h.run(document("return 41 + 1;", []))
    assert final["nodes"]["s"]["output"] == {"value": 42}
