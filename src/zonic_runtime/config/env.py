"""The process environment, validated once. The only module that reads ``os.environ`` (rule 4).

Endpoints, credentials and the environment name come from here; every tunable
value comes from ``resources/settings*.json`` instead.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

_REPO_ROOT = Path(__file__).resolve().parents[3]


class EnvironmentModel(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ZONIC_", env_file=None, extra="ignore", frozen=True)

    env: Literal["dev", "test", "prod"] = "dev"
    resources_dir: Path = _REPO_ROOT / "resources"
    instance_id: str = Field(default_factory=lambda: f"{os.getpid()}")
    internal_address: str = "http://127.0.0.1:9000"

    # pgfoundation gateway
    gateway_base_url: str = "http://127.0.0.1:8600"
    gateway_api_key: SecretStr | None = None

    # identity provider (OIDC)
    oidc_issuer: str | None = None
    oidc_audience: str | None = None
    oidc_jwks_url: str | None = None

    # model providers
    aws_region: str | None = None
    azure_openai_endpoint: str | None = None
    azure_openai_api_key: SecretStr | None = None
    openai_api_key: SecretStr | None = None
    anthropic_api_key: SecretStr | None = None

    # MCP server and cache service
    mcp_base_url: str | None = None
    cache_api_url: str | None = None

    # Langfuse (only when the optional server exporter is on)
    langfuse_public_key: SecretStr | None = None
    langfuse_secret_key: SecretStr | None = None
    langfuse_base_url: str | None = None

    # checkpoint encryption (design Q10)
    checkpoint_aes_key: SecretStr | None = None


@lru_cache(maxsize=1)
def get_environment() -> EnvironmentModel:
    return EnvironmentModel()


def secret_from_environment(name: str) -> str | None:
    """Value of the secret ``name`` from the environment provider (``ZONIC_SECRET_<NAME>``)."""
    key = "ZONIC_SECRET_" + name.upper().replace("-", "_").replace(".", "_")
    return os.environ.get(key)
