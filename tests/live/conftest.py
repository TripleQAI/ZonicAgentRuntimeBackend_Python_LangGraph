"""Live tests reach the gateway named by ``ZONIC_GATEWAY_BASE_URL`` with ``ZONIC_GATEWAY_API_KEY``.

``EnvironmentModel`` reads both from the process environment, else from the git-ignored
``.env`` (the deployed gateway's Function URL and the ``zonic-agent-runtime`` caller key),
so ``uv run pytest -m live`` targets the real gateway without exporting anything.
"""

from __future__ import annotations

from zonic_runtime.config.env import EnvironmentModel


def live_environment() -> EnvironmentModel:
    return EnvironmentModel(env="dev")
