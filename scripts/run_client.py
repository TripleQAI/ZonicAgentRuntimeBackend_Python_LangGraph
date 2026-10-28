"""The developer test client (design 3.6; plan P2-03, case A-15).

Uses only the published API: list the catalog, start a run, print its event stream, and follow
up in the same session. Interrupt answers (P4-02) and dataset paging (P3-05) join as those
endpoints land.

    uv run python -m scripts.run_client --base-url http://localhost:8000 --token "$TOKEN" --list
    uv run python -m scripts.run_client --token "$TOKEN" --workflow <id> --message "hello"

The token is a Cognito access token of the configured pool (``OIDC_*`` settings of the server).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any

import httpx

_DATA = "data: "


async def sse(response: httpx.Response) -> AsyncIterator[dict[str, Any]]:
    async for line in response.aiter_lines():
        if line.startswith(_DATA):
            yield json.loads(line[len(_DATA) :])


@dataclass
class RunTranscript:
    run_id: str = ""
    session_id: str = ""
    events: list[dict[str, Any]] = field(default_factory=list)

    @property
    def text(self) -> str:
        return "".join(e["data"]["text"] for e in self.events if e["type"] == "token.delta")

    @property
    def final(self) -> dict[str, Any] | None:
        return self.events[-1] if self.events else None


class RuntimeClient:
    def __init__(self, client: httpx.AsyncClient, token: str) -> None:
        self._client = client
        self._headers = {"Authorization": f"Bearer {token}"}

    async def catalog(self) -> list[dict[str, Any]]:
        response = await self._client.get("/v1/catalog/agents", headers=self._headers)
        response.raise_for_status()
        return list(response.json()["items"])

    async def run(
        self,
        text: str,
        *,
        workflow_id: str | None = None,
        session_id: str | None = None,
        on_event: Callable[[dict[str, Any]], None] | None = None,
    ) -> RunTranscript:
        body: dict[str, Any] = {"input": {"text": text}, "mode": "stream"}
        if workflow_id:
            body["agenticWorkflowId"] = workflow_id
        if session_id:
            body["sessionId"] = session_id
        transcript = RunTranscript()
        async with self._client.stream("POST", "/v1/runs", json=body, headers=self._headers) as response:
            if response.status_code != 200:
                await response.aread()
                raise RuntimeError(f"{response.status_code}: {response.text}")
            transcript.run_id = response.headers.get("x-run-id", "")
            transcript.session_id = response.headers.get("x-session-id", "")
            async for event in sse(response):
                transcript.events.append(event)
                if on_event is not None:
                    on_event(event)
        return transcript

    async def messages(self, session_id: str) -> list[dict[str, Any]]:
        response = await self._client.get(f"/v1/sessions/{session_id}/messages", headers=self._headers)
        response.raise_for_status()
        return list(response.json()["items"])


def _print(event: dict[str, Any]) -> None:
    kind = event["type"]
    if kind == "token.delta":
        sys.stdout.write(event["data"]["text"])
        sys.stdout.flush()
        return
    where = f" {event['nodeId']}" if event.get("nodeId") else ""
    sys.stdout.write(
        f"\n[{event['seq']}] {kind}{where} {json.dumps(event['data'], ensure_ascii=False)[:200]}\n"
    )


async def _main(args: argparse.Namespace) -> int:
    async with httpx.AsyncClient(base_url=args.base_url, timeout=None) as http:
        client = RuntimeClient(http, args.token)
        if args.list:
            for agent in await client.catalog():
                sys.stdout.write(
                    f"{agent['agenticWorkflowId']}  {agent.get('displayName') or agent.get('name', '')}\n"
                )
            return 0
        transcript = await client.run(
            args.message, workflow_id=args.workflow, session_id=args.session, on_event=_print
        )
        sys.stdout.write(f"\nrun {transcript.run_id} in session {transcript.session_id}\n")
        return 0 if transcript.final and transcript.final["type"] == "run.completed" else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--token", required=True, help="a Cognito access token")
    parser.add_argument("--list", action="store_true", help="list the workflows the caller may run")
    parser.add_argument("--workflow", help="agenticWorkflowId for a new session")
    parser.add_argument("--session", help="continue an existing session")
    parser.add_argument("--message", default="hello")
    return asyncio.run(_main(parser.parse_args(argv)))


if __name__ == "__main__":
    sys.exit(main())
