"""The load driver (plan P5-02): concurrent streaming runs against ``tests.load.server``.

    python -m tests.load.drive --users 150 --runs 3 --out q05.json

Each user is its own owner (``load-test-NNN``) in one of ``--tenants`` tenants, so the per-user and
per-tenant caps (5 and 50) do not limit the concurrency under test; all users start at once and
run ``--runs`` turns one after another, each a new session. The driver samples ``/metrics`` on the
internal port while it runs and writes one JSON report.

Measured per run: the time from the request to ``run.started`` and to the first token; with
``--stamps`` (the driver on the same host as the server, so the clocks agree), the latency each
token frame adds after its last chunk was emitted; whether every event belongs to the run; the
final event. k6 is not used: it cannot read an SSE body as it streams.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import statistics
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import jwt
from cryptography.hazmat.primitives import serialization

from tests.load.model import stamps
from tests.load.server import AUDIENCE, ISSUER, KID, OWNER_PREFIX

_DATA = "data: "


def token(signer: Any, subject: str, tenant: str) -> str:
    now = int(time.time())
    claims = {"sub": subject, "client_id": AUDIENCE, "iss": ISSUER, "iat": now, "exp": now + 3600}
    claims["custom:tenant"] = tenant
    return jwt.encode(claims, signer, algorithm="RS256", headers={"kid": KID})


@dataclass
class RunSample:
    status: int = 0
    final: str = ""
    started_ms: float | None = None
    first_token_ms: float | None = None
    total_ms: float = 0.0
    frames: int = 0
    added_ms: list[float] = field(default_factory=list)
    crossed: bool = False
    error: str = ""


async def one_run(
    client: httpx.AsyncClient, headers: dict[str, str], workflow: str, measure: bool
) -> RunSample:
    sample = RunSample()
    body = {
        "agenticWorkflowId": workflow,
        "input": {"text": "How are the facilities doing?"},
        "mode": "stream",
    }
    begin = time.perf_counter()
    try:
        async with client.stream("POST", "/v1/runs", json=body, headers=headers) as response:
            sample.status = response.status_code
            if response.status_code != 200:
                sample.error = (await response.aread()).decode()[:200]
                return sample
            run_id = response.headers.get("x-run-id", "")
            async for line in response.aiter_lines():
                if not line.startswith(_DATA):
                    continue
                arrived_ns = time.time_ns()
                event = json.loads(line[len(_DATA) :])
                elapsed = (time.perf_counter() - begin) * 1000
                sample.crossed |= event.get("runId") != run_id
                kind = event["type"]
                if kind == "run.started":
                    sample.started_ms = elapsed
                elif kind == "token.delta":
                    sample.frames += 1
                    if sample.first_token_ms is None:
                        sample.first_token_ms = elapsed
                    found = stamps(event["data"]["text"])
                    if measure and found:
                        sample.added_ms.append((arrived_ns - found[-1]) / 1e6)
                sample.final = kind
    except httpx.HTTPError as exc:
        sample.error = type(exc).__name__
    sample.total_ms = (time.perf_counter() - begin) * 1000
    return sample


async def sampler(metrics: str, stop: asyncio.Event, into: list[dict[str, float]]) -> None:
    async with httpx.AsyncClient(timeout=5) as client:
        while not stop.is_set():
            try:
                text = (await client.get(metrics)).text
                values: dict[str, float] = {"t": time.time()}
                for line in text.splitlines():
                    if line.startswith("zonic_") and "{" not in line:
                        name, _, value = line.partition(" ")
                        values[name.removeprefix("zonic_")] = float(value)
                into.append(values)
            except httpx.HTTPError:
                pass
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), 0.5)


def percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[min(len(ordered) - 1, int(fraction * len(ordered)))], 2)


def summary(values: list[float]) -> dict[str, float | None]:
    return {
        "p50": percentile(values, 0.5),
        "p95": percentile(values, 0.95),
        "p99": percentile(values, 0.99),
        "max": round(max(values), 2) if values else None,
        "mean": round(statistics.fmean(values), 2) if values else None,
    }


async def drive(args: argparse.Namespace, key: bytes) -> dict[str, Any]:
    signer = serialization.load_pem_private_key(key, password=None)
    limits = httpx.Limits(max_connections=args.users + 10, max_keepalive_connections=args.users + 10)
    async with httpx.AsyncClient(base_url=args.base_url, timeout=None, limits=limits) as client:
        headers0 = {"Authorization": f"Bearer {token(signer, f'{OWNER_PREFIX}000', 'load-test-0')}"}
        catalog = (await client.get("/v1/catalog/agents", headers=headers0)).json()["items"]
        workflow = next(a["agenticWorkflowId"] for a in catalog if a["name"] == args.workflow)
        users = [
            {
                "Authorization": "Bearer "
                + token(signer, f"{OWNER_PREFIX}{i:03d}", f"load-test-{i % args.tenants}")
            }
            for i in range(1, args.users + 1)
        ]
        metrics: list[dict[str, float]] = []
        stop = asyncio.Event()
        watcher = asyncio.create_task(sampler(args.metrics, stop, metrics))
        await asyncio.sleep(1.0)  # a baseline sample before the load

        async def user(position: int, headers: dict[str, str]) -> list[RunSample]:
            await asyncio.sleep(args.ramp_seconds * position / len(users))
            return [await one_run(client, headers, workflow, args.stamps) for _ in range(args.runs)]

        began = time.perf_counter()
        results = await asyncio.gather(*(user(i, h) for i, h in enumerate(users)))
        wall = time.perf_counter() - began
        await asyncio.sleep(1.0)
        stop.set()
        await watcher
    runs = [r for per_user in results for r in per_user]
    statuses: dict[str, int] = {}
    for r in runs:
        key = f"{r.status} {r.final or r.error}"
        statuses[key] = statuses.get(key, 0) + 1
    baseline = metrics[0] if metrics else {}
    peak_active = max((m.get("active_runs", 0) for m in metrics), default=0)
    peak_rss = max((m.get("resident_memory_megabytes", 0) for m in metrics), default=0)
    return {
        "users": args.users,
        "runs_per_user": args.runs,
        "tenants": args.tenants,
        "ramp_seconds": args.ramp_seconds,
        "wall_seconds": round(wall, 2),
        "runs": len(runs),
        "statuses": statuses,
        "crossed_runs": sum(r.crossed for r in runs),
        "run_started_ms": summary([r.started_ms for r in runs if r.started_ms is not None]),
        "first_token_ms": summary([r.first_token_ms for r in runs if r.first_token_ms is not None]),
        "total_ms": summary([r.total_ms for r in runs if r.status == 200]),
        "frames_per_run": summary([float(r.frames) for r in runs if r.status == 200]),
        "token_added_ms": summary([v for r in runs for v in r.added_ms]) if args.stamps else None,
        "peak_active_runs": peak_active,
        "event_loop_lag_p99_ms": summary(
            [m["event_loop_lag_p99_milliseconds"] for m in metrics if "event_loop_lag_p99_milliseconds" in m]
        ),
        "event_loop_lag_ms": summary(
            [m["event_loop_lag_milliseconds"] for m in metrics if "event_loop_lag_milliseconds" in m]
        ),
        "rss_baseline_mb": baseline.get("resident_memory_megabytes"),
        "rss_peak_mb": peak_rss,
        "rss_per_active_run_mb": (
            round((peak_rss - baseline.get("resident_memory_megabytes", peak_rss)) / peak_active, 3)
            if peak_active
            else None
        ),
        "overloaded_samples": sum(1 for m in metrics if m.get("overloaded")),
        "errors": [r.error for r in runs if r.error][:5],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--base-url", default="http://127.0.0.1:18000")
    parser.add_argument("--metrics", default="http://127.0.0.1:19000/metrics")
    parser.add_argument("--key", default="tests/load/.keys/key.pem")
    parser.add_argument("--workflow", default="test-agent-workflow")
    parser.add_argument("--users", type=int, default=150)
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--tenants", type=int, default=5)
    parser.add_argument("--ramp-seconds", type=float, default=0.0, help="spread the users' starts over this")
    parser.add_argument("--stamps", action="store_true", help="measure the latency added per token frame")
    parser.add_argument("--out")
    args = parser.parse_args(argv)
    report = asyncio.run(drive(args, Path(args.key).read_bytes()))
    text = json.dumps(report, indent=2)
    if args.out:
        Path(args.out).write_text(text + "\n", encoding="utf-8")
    sys.stdout.write(text + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
