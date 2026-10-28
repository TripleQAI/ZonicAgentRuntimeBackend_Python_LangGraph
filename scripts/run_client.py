"""The developer test client (design 3.6; plan P2-03, P5-05, case A-15).

Uses only the published API: list the catalog, start a run, print its event stream, follow up in
the same session, answer the run's interrupts from the terminal and page a dataset.

    uv run python -m scripts.run_client --base-url http://localhost:8000 --token "$TOKEN" --list
    uv run python -m scripts.run_client --token "$TOKEN" --workflow <id> --message "hello"
    uv run python -m scripts.run_client --token "$TOKEN" --session <id> --message "and then?"
    uv run python -m scripts.run_client --token "$TOKEN" --workflow <id> --answer '{"approved": true}'
    uv run python -m scripts.run_client --token "$TOKEN" --dataset <datasetId> --page 2 --page-size 50

A paused run (``run.waiting``) is resumed with ``--answer`` (JSON, for every interrupt) or, at a
terminal, with the answer typed at the prompt. The token is a Cognito access token of the
configured pool (``OIDC_*`` settings of the server). See ``docs/runbooks/test-client.md``.
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
_WAITING = "run.waiting"
_COMPLETED = "run.completed"
_DATASET_ID = "datasetId"

EventHandler = Callable[[dict[str, Any]], None]
#: ``interrupt -> answer``, or ``None`` to leave the run waiting.
Answerer = Callable[[dict[str, Any]], Any]


async def sse(response: httpx.Response) -> AsyncIterator[dict[str, Any]]:
    async for line in response.aiter_lines():
        if line.startswith(_DATA):
            yield json.loads(line[len(_DATA) :])


def _dataset_ids(value: Any, found: list[str]) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if key == _DATASET_ID and isinstance(item, str) and item not in found:
                found.append(item)
            else:
                _dataset_ids(item, found)
    elif isinstance(value, list):
        for item in value:
            _dataset_ids(item, found)


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

    @property
    def waiting(self) -> bool:
        return self.final is not None and self.final["type"] == _WAITING

    @property
    def dataset_ids(self) -> list[str]:
        """The datasets the run's events name (grid parts, results), for ``--dataset``."""
        found: list[str] = []
        _dataset_ids([e.get("data") for e in self.events], found)
        return found


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
        on_event: EventHandler | None = None,
    ) -> RunTranscript:
        body: dict[str, Any] = {"input": {"text": text}, "mode": "stream"}
        if workflow_id:
            body["agenticWorkflowId"] = workflow_id
        if session_id:
            body["sessionId"] = session_id
        return await self._stream("/v1/runs", body, on_event)

    async def interrupts(self, run_id: str) -> list[dict[str, Any]]:
        response = await self._client.get(f"/v1/runs/{run_id}/interrupts", headers=self._headers)
        response.raise_for_status()
        return list(response.json()["items"])

    async def answer(
        self, run_id: str, interrupt_id: str, answer: Any, *, on_event: EventHandler | None = None
    ) -> RunTranscript:
        """Answers one interrupt; the run resumes in stream mode, and this is its stream."""
        body = {"answer": answer, "mode": "stream"}
        return await self._stream(f"/v1/runs/{run_id}/interrupts/{interrupt_id}", body, on_event)

    async def resume(
        self, waiting: RunTranscript, answerer: Answerer, *, on_event: EventHandler | None = None
    ) -> RunTranscript:
        """Answers a paused run's interrupts until it ends or ``answerer`` gives no answer."""
        transcript = waiting
        while transcript.waiting:
            pending = await self.interrupts(transcript.run_id)
            if not pending:
                break
            value = answerer(pending[0])
            if value is None:
                break
            resumed = await self.answer(
                transcript.run_id, pending[0]["interruptId"], value, on_event=on_event
            )
            transcript = RunTranscript(
                run_id=resumed.run_id or transcript.run_id,
                session_id=resumed.session_id or transcript.session_id,
                events=[*transcript.events, *resumed.events],
            )
        return transcript

    async def dataset(
        self,
        dataset_id: str,
        *,
        page: int | None = None,
        page_size: int | None = None,
        sort: str | None = None,
        filter: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """One page of a dataset; ``sort`` is ``column`` or ``column:desc``, ``filter`` maps
        columns to the text their values must contain."""
        params: dict[str, Any] = {"page": page, "pageSize": page_size, "sort": sort}
        if filter is not None:
            params["filter"] = json.dumps(filter)
        response = await self._client.get(
            f"/v1/datasets/{dataset_id}",
            params={k: v for k, v in params.items() if v is not None},
            headers=self._headers,
        )
        response.raise_for_status()
        return dict(response.json())

    async def messages(self, session_id: str) -> list[dict[str, Any]]:
        response = await self._client.get(f"/v1/sessions/{session_id}/messages", headers=self._headers)
        response.raise_for_status()
        return list(response.json()["items"])

    async def _stream(self, path: str, body: dict[str, Any], on_event: EventHandler | None) -> RunTranscript:
        transcript = RunTranscript()
        async with self._client.stream("POST", path, json=body, headers=self._headers) as response:
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


def _answerer(given: str | None) -> Answerer:
    """``--answer`` for every interrupt; else the answer typed at the terminal; else none."""
    if given is not None:
        value = json.loads(given)
        return lambda _: value

    def ask(interrupt: dict[str, Any]) -> Any:
        sys.stdout.write(
            f"\ninterrupt {interrupt['interruptId']} ({interrupt['kind']}) at {interrupt['nodeId']}\n"
            f"  {interrupt.get('prompt', {}).get('text', '')}\n"
            f"  answer schema: {json.dumps(interrupt.get('answerSchema', {}))}\n"
        )
        if not sys.stdin.isatty():
            return None
        typed = input("answer (JSON, empty to stop)> ").strip()
        return json.loads(typed) if typed else None

    return ask


async def _main(args: argparse.Namespace) -> int:
    async with httpx.AsyncClient(base_url=args.base_url, timeout=None) as http:
        client = RuntimeClient(http, args.token)
        if args.list:
            for agent in await client.catalog():
                sys.stdout.write(
                    f"{agent['agenticWorkflowId']}  {agent.get('displayName') or agent.get('name', '')}\n"
                )
            return 0
        if args.dataset:
            page = await client.dataset(
                args.dataset,
                page=args.page,
                page_size=args.page_size,
                sort=args.sort,
                filter=json.loads(args.filter) if args.filter else None,
            )
            sys.stdout.write(json.dumps(page, ensure_ascii=False, indent=2) + "\n")
            return 0
        transcript = await client.run(
            args.message, workflow_id=args.workflow, session_id=args.session, on_event=_print
        )
        transcript = await client.resume(transcript, _answerer(args.answer), on_event=_print)
        sys.stdout.write(f"\nrun {transcript.run_id} in session {transcript.session_id}\n")
        for dataset_id in transcript.dataset_ids:
            sys.stdout.write(f"dataset {dataset_id}\n")
        if transcript.waiting:
            sys.stdout.write("the run is waiting for an answer\n")
        return 0 if transcript.final and transcript.final["type"] == _COMPLETED else 1


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
    parser.add_argument("--answer", help="JSON answer for every interrupt of the run")
    parser.add_argument("--dataset", help="print a page of this dataset instead of running")
    parser.add_argument("--page", type=int)
    parser.add_argument("--page-size", type=int)
    parser.add_argument("--sort", help="column, or column:desc")
    parser.add_argument(
        "--filter", help='JSON object of column to contained text, e.g. {"borough": "queens"}'
    )
    return asyncio.run(_main(parser.parse_args(argv)))


if __name__ == "__main__":
    sys.exit(main())
