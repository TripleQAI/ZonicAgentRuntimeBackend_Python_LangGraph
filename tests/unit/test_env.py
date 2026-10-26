"""``EnvironmentModel`` reads ``ZONIC_*`` from the process, then from ``.env``."""

from __future__ import annotations

from pathlib import Path

import pytest

from zonic_runtime.config.env import EnvironmentModel

GATEWAY = "https://gateway.example.test"


def _dotenv(tmp_path: Path) -> Path:
    path = tmp_path / ".env"
    path.write_text(
        f"ZONIC_GATEWAY_BASE_URL={GATEWAY}\nZONIC_GATEWAY_API_KEY=k-file\nAGENTRUNTIME_DSN=ignored\n",
        encoding="utf-8",
    )
    return path


def test_gateway_settings_come_from_the_env_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ZONIC_GATEWAY_BASE_URL", raising=False)
    monkeypatch.delenv("ZONIC_GATEWAY_API_KEY", raising=False)
    env = EnvironmentModel(_env_file=_dotenv(tmp_path))  # type: ignore[call-arg]
    assert env.gateway_base_url == GATEWAY
    assert env.gateway_api_key is not None and env.gateway_api_key.get_secret_value() == "k-file"


def test_process_environment_wins_over_the_env_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ZONIC_GATEWAY_BASE_URL", "https://from-process.test")
    env = EnvironmentModel(_env_file=_dotenv(tmp_path))  # type: ignore[call-arg]
    assert env.gateway_base_url == "https://from-process.test"


def test_without_an_env_file_the_local_default_applies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ZONIC_GATEWAY_BASE_URL", raising=False)
    env = EnvironmentModel(_env_file=tmp_path / "missing.env")  # type: ignore[call-arg]
    assert env.gateway_base_url == EnvironmentModel.model_fields["gateway_base_url"].default
