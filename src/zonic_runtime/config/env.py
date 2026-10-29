"""The process environment, validated once. The only module that reads ``os.environ`` (rule 4).

Endpoints, credentials and the environment name come from here; every tunable
value comes from ``resources/settings*.json`` instead. ``ZONIC_*`` variables are read
from the process environment, then from the repository's git-ignored ``.env`` (see
``.env.example``); a variable set in the process wins, so deployments set them directly.
"""

from __future__ import annotations

import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

_REPO_ROOT = Path(__file__).resolve().parents[3]


#: Where the Langfuse client would send traces if it ever did: export goes to the runtime's own
#: span exporter (design 12), so this address is never contacted.
NO_LANGFUSE_SERVER = "http://127.0.0.1:9"


def internal_address(host: str, port: int) -> str:
    """This instance's internal port as other instances reach it (the default of ``api.internalAddress``)."""
    return f"http://{host}:{port}"


class EnvironmentModel(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="ZONIC_",
        env_file=_REPO_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
    )

    env: Literal["dev", "test", "prod"] = "dev"
    resources_dir: Path = _REPO_ROOT / "resources"
    instance_id: str = Field(default_factory=lambda: f"{os.getpid()}")
    internal_address: str = "http://127.0.0.1:9000"
    #: The public port (``zonic_runtime.server``); the internal one is ``api.internalPort``.
    port: int = 8000
    host: str = "0.0.0.0"

    # pgfoundation gateway: the deployed Lambda (tripleq-development), never a local one.
    # Other environments set ZONIC_GATEWAY_BASE_URL.
    gateway_base_url: str = "https://sdyvf4y53x524cwgdisy2qyfqy0bgjkm.lambda-url.us-east-1.on.aws"
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
    #: Nebius Token Factory (OpenAI-compatible): its NVIDIA Nemotron models name these fields
    #: (``endpointEnv``, ``apiKeyEnv`` in ``models.json``).
    nebius_base_url: str = "https://api.tokenfactory.nebius.com/v1/"
    nebius_api_key: SecretStr | None = None

    # MCP server and cache service
    mcp_base_url: str | None = None
    #: The MCP server's bearer token (its ``MCP_AUTH_TOKEN``); ``mcp-servers.json`` names it.
    mcp_auth_token: SecretStr | None = None
    cache_api_url: str | None = None

    # the Script node's sandbox (a Piston API); unset: Script nodes are FEATURE_DISABLED
    sandbox_url: str | None = None

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


def environment_secret(name: str) -> str | None:
    """A ``secret:<name>`` from ``ZONIC_SECRET_<NAME>`` (the secret port's local adapter)."""
    return os.environ.get("ZONIC_SECRET_" + re.sub(r"[^A-Za-z0-9]", "_", name).upper()) or None
