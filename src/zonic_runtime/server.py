"""``python -m zonic_runtime.server``: one process, one event loop, two ports (design AD-11).

The public app on ``ZONIC_PORT`` (8000) and the internal app (``/metrics``) on ``api.internalPort``
share the process's ``AppDeps``, so the gauges read the runs this process executes. The public
app's lifespan starts and closes the dependencies; a signal stops both servers.
"""

from __future__ import annotations

import asyncio
import contextlib
import sys
from collections.abc import Callable

import uvicorn

from zonic_runtime.api.deps import AppDeps
from zonic_runtime.api.internal import create_internal_app
from zonic_runtime.config.env import EnvironmentModel
from zonic_runtime.main import create_app
from zonic_runtime.observability import logging_policy

_GRACEFUL_SECONDS = 60


async def serve() -> None:
    logging_policy.install()
    env = EnvironmentModel()
    deps = AppDeps.from_environment(env)
    public = uvicorn.Server(
        uvicorn.Config(
            create_app(deps), host=env.host, port=env.port, timeout_graceful_shutdown=_GRACEFUL_SECONDS
        )
    )
    internal = uvicorn.Server(
        uvicorn.Config(
            create_internal_app(deps),
            host=env.host,
            port=deps.resources.settings.api.internal_port,
            lifespan="off",
        )
    )
    # Only the public server takes the signals; its exit stops the internal one.
    internal.capture_signals = contextlib.nullcontext  # type: ignore[method-assign,assignment]
    task = asyncio.create_task(internal.serve())
    try:
        await public.serve()
    finally:
        internal.should_exit = True
        await task


def _loop_factory() -> Callable[[], asyncio.AbstractEventLoop] | None:
    if sys.platform == "win32":
        return None
    import uvloop  # uvicorn[standard] installs it off Windows

    return uvloop.new_event_loop


def main() -> None:
    asyncio.run(serve(), loop_factory=_loop_factory())


if __name__ == "__main__":
    main()
