"""Shared clients and the blocking-call executor (design 11.2, "Connection reuse", "Blocking
clients"; plan P2-05, case Q-09).

* ``HttpPools``: one ``httpx.AsyncClient`` per upstream (the gateway now; MCP, the cache service
  and egress later), created on first use and closed with the app.
* ``BedrockClients``: one boto3 ``bedrock-runtime`` client per region, shared by every
  ``ChatBedrockConverse`` (boto3 clients are thread-safe). Its connection pool has
  ``models.executorThreads`` connections, one per thread that may call it.
* ``blocking_executor``: the event loop's default executor, a bounded ``ThreadPoolExecutor`` of
  ``models.executorThreads``. LangChain runs a blocking client's calls (boto3 under
  ``ChatBedrockConverse``) through ``run_in_executor(None, ...)``, so this bounds them.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import httpx

from zonic_runtime.models.resources.settings import ModelRuntimeSettings

_BEDROCK_SERVICE = "bedrock-runtime"
_THREAD_PREFIX = "zonic-blocking"


class HttpPools:
    def __init__(self) -> None:
        self._clients: dict[str, httpx.AsyncClient] = {}

    def get(self, upstream: str, create: Callable[[], httpx.AsyncClient]) -> httpx.AsyncClient:
        found = self._clients.get(upstream)
        if found is None or found.is_closed:
            found = self._clients[upstream] = create()
        return found

    @property
    def upstreams(self) -> list[str]:
        return sorted(self._clients)

    async def aclose(self) -> None:
        clients, self._clients = list(self._clients.values()), {}
        await asyncio.gather(*(c.aclose() for c in clients), return_exceptions=True)


def _boto3_client(region: str | None, settings: ModelRuntimeSettings) -> Any:
    """botocore sets what ``ChatBedrockConverse`` is not given: the read timeout and the
    retries (``standard`` mode backs off on throttling, case R-02)."""
    import boto3  # only where Bedrock is used
    from botocore.config import Config

    config = Config(
        max_pool_connections=settings.executor_threads,
        read_timeout=settings.request_timeout_seconds,
        retries={"max_attempts": settings.max_retries + 1, "mode": "standard"},
    )
    return boto3.client(_BEDROCK_SERVICE, region_name=region, config=config)


class BedrockClients:
    """``__call__(region)`` is the ``BedrockClient`` the model factory takes."""

    def __init__(
        self,
        settings: ModelRuntimeSettings,
        create: Callable[[str | None, ModelRuntimeSettings], Any] = _boto3_client,
    ) -> None:
        self._settings = settings
        self._create = create
        self._clients: dict[str | None, Any] = {}

    def __call__(self, region: str | None) -> Any:
        found = self._clients.get(region)
        if found is None:
            found = self._clients[region] = self._create(region, self._settings)
        return found

    @property
    def regions(self) -> list[str | None]:
        return list(self._clients)


def blocking_executor(settings: ModelRuntimeSettings) -> ThreadPoolExecutor:
    return ThreadPoolExecutor(max_workers=settings.executor_threads, thread_name_prefix=_THREAD_PREFIX)


def install_blocking_executor(settings: ModelRuntimeSettings) -> ThreadPoolExecutor:
    """Makes ``blocking_executor`` the running loop's default executor (at app start)."""
    executor = blocking_executor(settings)
    asyncio.get_running_loop().set_default_executor(executor)
    return executor
