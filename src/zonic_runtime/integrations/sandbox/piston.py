"""``PistonSandbox``: Script nodes on a Piston API (``POST /api/v2/execute``; plan P3-07, case N-17).

The node's code becomes a program (``wrap_script``): ``input`` holds the resolved inputs, the
code is a function body (Script nodes ``return`` their result), and the return value is printed
as JSON after ``RESULT_MARKER``. The inputs travel as one JSON string literal, never as code.
One execution per visit; its run timeout is the smaller of the node's and
``sandbox.runTimeoutMilliseconds``; output above ``sandbox.maxOutputBytes`` is cut. The wrapper,
the marker and the failures are the Node.js runtime's, so both runtimes run a script the same way.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

import httpx

from zonic_runtime.errors import ZonicError
from zonic_runtime.models.resources.settings import SandboxSettings
from zonic_runtime.ports.sandbox import SandboxRequest, SandboxResult

RESULT_MARKER = "__ZONIC_RESULT__"
ERROR = "SANDBOX_ERROR"
_PYTHON = "python"
_EXECUTE = "/api/v2/execute"
_REASON_CHARS = 500


def wrap_script(language: str, code: str, given: Mapping[str, Any]) -> str:
    literal = json.dumps(json.dumps(dict(given), ensure_ascii=False, default=str))
    marker = json.dumps(RESULT_MARKER)
    if language == _PYTHON:
        body = "\n".join(f"    {line}" for line in code.split("\n"))
        return "\n".join(
            [
                "import json",
                f"input = json.loads({literal})",
                "def __zonic_run(input):",
                body if code.strip() else "    return None",
                "print()",
                f"print({marker} + json.dumps(__zonic_run(input), default=str))",
            ]
        )
    return "\n".join(
        [
            f"const input = JSON.parse({literal});",
            f"const __zonicRun = async (input) => {{\n{code}\n}};",
            "__zonicRun(input).then(",
            "  (value) => process.stdout.write(",
            f"    '\\n' + {marker} + JSON.stringify(value === undefined ? null : value) + '\\n'),",
            "  (error) => {",
            "    process.stderr.write(String((error && error.stack) || error));",
            "    process.exit(1);",
            "  },",
            ");",
        ]
    )


def script_result(stdout: str) -> tuple[bool, Any, str]:
    """``(found, result, the output before the marker)``; raises ``ValueError`` on bad JSON."""
    index = stdout.rfind(RESULT_MARKER)
    if index < 0:
        return False, None, stdout
    line = stdout[index + len(RESULT_MARKER) :].split("\n")[0]
    return True, json.loads(line), stdout[:index].rstrip()


class PistonSandbox:
    def __init__(
        self,
        settings: SandboxSettings,
        base_url: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._settings = settings
        self._url = base_url.rstrip("/") + _EXECUTE
        self._transport = transport
        self._client: httpx.AsyncClient | None = None

    def _http(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(transport=self._transport, trust_env=False)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()

    async def run(self, request: SandboxRequest) -> SandboxResult:
        runtime = self._settings.languages.get(request.language)
        if runtime is None:
            raise ZonicError(ERROR, reason=f"language {request.language} is not supported")
        timeout_ms = min(int(request.timeout_seconds * 1000), self._settings.run_timeout_milliseconds)
        body = {
            "language": runtime,
            "version": "*",
            "files": [{"content": wrap_script(request.language, request.code, request.input)}],
            "run_timeout": timeout_ms,
        }
        try:
            # The HTTP wait covers the run plus Piston's own start-up and compile time.
            response = await self._http().post(self._url, json=body, timeout=timeout_ms / 1000 + 10)
        except httpx.HTTPError as exc:
            raise ZonicError(ERROR, transport=True, reason="sandbox unreachable") from exc
        if response.status_code >= 400:
            raise ZonicError(ERROR, status=response.status_code, reason=response.text[:_REASON_CHARS])
        try:
            answer = response.json()
        except ValueError as exc:
            raise ZonicError(ERROR, reason="the sandbox answer is not JSON") from exc
        run = answer.get("run") if isinstance(answer, dict) else None
        run = run if isinstance(run, dict) else {}
        limit = self._settings.max_output_bytes
        stdout, stderr = str(run.get("stdout") or "")[:limit], str(run.get("stderr") or "")[:limit]
        signal = run.get("signal")
        if isinstance(signal, str) and signal:  # Piston kills a script past its run timeout
            raise ZonicError(ERROR, reason=f"script stopped by {signal}", timeout_ms=timeout_ms)
        code = run.get("code")
        if isinstance(code, int) and code != 0:
            raise ZonicError(ERROR, reason=stderr[:_REASON_CHARS] or f"exit code {code}")
        try:
            found, result, output = script_result(stdout)
        except ValueError as exc:
            raise ZonicError(ERROR, reason="the script result is not JSON") from exc
        if not found:
            raise ZonicError(ERROR, reason="the script printed no result")
        return SandboxResult(result=result, stdout=output, stderr=stderr)
