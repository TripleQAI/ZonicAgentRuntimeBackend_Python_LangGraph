"""Live tests reach the gateway named by ``ZONIC_GATEWAY_BASE_URL`` with ``ZONIC_GATEWAY_API_KEY``.

Each comes from the environment, else from the git-ignored ``.env`` (the deployed
gateway's Function URL and the ``zonic-agent-runtime`` caller key), so
``uv run pytest -m live`` targets the real gateway without exporting anything.
"""

from __future__ import annotations

import os
from pathlib import Path

from pydantic import SecretStr

from zonic_runtime.config.env import EnvironmentModel

ROOT = Path(__file__).resolve().parents[2]


def _setting(name: str) -> str | None:
    if os.environ.get(name):
        return os.environ[name]
    path = ROOT / ".env"
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            key, sep, value = line.partition("=")
            if sep and key.strip() == name:
                return value.strip()
    return None


def live_environment() -> EnvironmentModel:
    key = _setting("ZONIC_GATEWAY_API_KEY")
    return EnvironmentModel(
        env="dev",
        gateway_base_url=_setting("ZONIC_GATEWAY_BASE_URL") or EnvironmentModel().gateway_base_url,
        gateway_api_key=SecretStr(key) if key else None,
    )
