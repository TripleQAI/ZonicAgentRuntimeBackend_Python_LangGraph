"""The runtime under load (plan P5-02): ``python -m tests.load.server``, in the load container of
1 vCPU and 2 GB (``tests/load/Dockerfile``; ``docs/load/p5-02-report.md``).

Real: the deployed gateway (``ZONIC_GATEWAY_*``), the trace export to ``agent_observability``,
admission, permits, pressure, the event bus and SSE: everything ``zonic_runtime.server`` runs,
except two stand-ins.

* The model: ``StampedChatModel`` (``LOAD_TOKENS`` chunks, the first after
  ``LOAD_FIRST_TOKEN_SECONDS``, then ``LOAD_TOKEN_RATE`` a second), for every catalog model.
* The identity provider: tokens signed with the key in ``LOAD_KEY_FILE`` (generated if missing),
  whose owners are ``load-test-NNN``; ``tests.load.drive`` signs them.

Runs are not durable here (no claimer), so the server never claims another instance's queued runs.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from dataclasses import replace
from pathlib import Path

import uvicorn
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey

from tests.kinds.agents import ScriptedModels
from tests.load.model import stamped
from zonic_runtime.api.auth import StaticKeys, TokenVerifier
from zonic_runtime.api.deps import AppDeps
from zonic_runtime.api.internal import create_internal_app
from zonic_runtime.config.env import EnvironmentModel
from zonic_runtime.config.settings import RuntimeResources, load_resources
from zonic_runtime.engine.agents.agent_factory import AgentServices
from zonic_runtime.integrations.gateway.http_gateway import HttpGateway
from zonic_runtime.integrations.langfuse.tracing import GatewayTracing
from zonic_runtime.main import create_app
from zonic_runtime.observability import logging_policy

ISSUER, AUDIENCE, KID = "https://load.zonic.test", "load", "load-key"
OWNER_PREFIX = "load-test-"


def key(path: Path) -> RSAPrivateKey:
    if path.exists():
        loaded = serialization.load_pem_private_key(path.read_bytes(), password=None)
        assert isinstance(loaded, RSAPrivateKey)
        return loaded
    created = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(
        created.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
        )
    )
    return created


def load_resources_for(env: EnvironmentModel) -> RuntimeResources:
    """The environment's resources with every catalog model enabled (the stamped model answers),
    each with a quota of ``LOAD_MODEL_IN_FLIGHT`` calls (500): the simulated model is not the
    bottleneck under test (the governor's own queueing is R-02's, offline)."""
    found = load_resources(env)
    in_flight = int(os.environ.get("LOAD_MODEL_IN_FLIGHT", "500"))
    quota = {"max_in_flight": in_flight, "requests_per_minute": in_flight * 60, "tokens_per_minute": 10**9}
    models = {
        code: e.model_copy(update={"enabled": True, "quota": e.quota.model_copy(update=quota)})
        for code, e in found.models.models.items()
    }
    return replace(found, models=found.models.model_copy(update={"models": models}))


def build(env: EnvironmentModel) -> AppDeps:
    resources = load_resources_for(env)
    signer = key(Path(os.environ.get("LOAD_KEY_FILE", "/load/key.pem")))
    verifier = TokenVerifier(
        StaticKeys({KID: signer.public_key()}), resources.settings.auth, issuer=ISSUER, audience=AUDIENCE
    )
    model = stamped(
        int(os.environ.get("LOAD_TOKENS", "50")),
        float(os.environ.get("LOAD_FIRST_TOKEN_SECONDS", "0.3")),
        float(os.environ.get("LOAD_TOKEN_RATE", "50")),
    )
    gateway = HttpGateway(resources.settings.gateway, env)
    return AppDeps.build(
        resources,
        gateway,
        verifier,
        agents=AgentServices.from_resources(resources, builders=ScriptedModels(model).builders()),
        tracing=GatewayTracing.create(resources, gateway),
        release="load",
    )


async def serve() -> None:
    logging_policy.install()
    env = EnvironmentModel()
    deps = build(env)
    public = uvicorn.Server(uvicorn.Config(create_app(deps), host=env.host, port=env.port))
    internal = uvicorn.Server(
        uvicorn.Config(
            create_internal_app(deps),
            host=env.host,
            port=deps.resources.settings.api.internal_port,
            lifespan="off",
        )
    )
    internal.capture_signals = contextlib.nullcontext  # type: ignore[method-assign,assignment]
    task = asyncio.create_task(internal.serve())
    try:
        await public.serve()
    finally:
        internal.should_exit = True
        await task


if __name__ == "__main__":
    import uvloop

    asyncio.run(serve(), loop_factory=uvloop.new_event_loop)
