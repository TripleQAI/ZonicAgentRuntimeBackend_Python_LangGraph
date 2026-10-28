"""``create_app``: the FastAPI app (design 14; plan P2-03).

``create_app(deps)`` serves a composed ``AppDeps``; ``create_app()`` without dependencies serves
nothing and exists for the OpenAPI document (``scripts/gen.py``). ``app_factory`` builds the
dependencies from the environment for ``uvicorn zonic_runtime.main:app_factory --factory``.
Interactive API docs only when ``api.docsEnabled`` (development settings).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from zonic_runtime.api import errors
from zonic_runtime.api.deps import AppDeps
from zonic_runtime.api.routes import catalog, datasets, health, observability, runs, sessions
from zonic_runtime.config.env import EnvironmentModel
from zonic_runtime.config.settings import RuntimeResources, load_resources
from zonic_runtime.models.events import RunEvent
from zonic_runtime.observability import logging_policy

TITLE = "Zonic Agent Runtime"
VERSION = "0.1.0"
_V1 = "/v1"


def create_app(deps: AppDeps | None = None, *, resources: RuntimeResources | None = None) -> FastAPI:
    resources = deps.resources if deps is not None else resources or load_resources(EnvironmentModel())
    api = resources.settings.api

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        if deps is not None:
            deps.start()
            await deps.warm()
        yield
        if deps is not None:
            await deps.aclose()

    docs = api.docs_enabled and deps is not None
    app = FastAPI(
        title=TITLE,
        version=VERSION,
        lifespan=lifespan,
        docs_url="/docs" if docs else None,
        redoc_url=None,
        openapi_url="/openapi.json" if docs else None,
    )
    app.state.deps = deps
    errors.install(app, resources.errors)
    if api.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(api.cors_origins),
            allow_methods=["GET", "POST", "PATCH"],
            allow_headers=[
                "authorization",
                "content-type",
                "idempotency-key",
                "last-event-id",
                "if-none-match",
            ],
            expose_headers=["etag", "retry-after", "x-request-id", "x-run-id", "x-session-id"],
        )
    app.add_middleware(errors.RequestContext, catalog=resources.errors, max_body_bytes=api.max_body_bytes)
    app.include_router(health.router)
    for router in (catalog.router, sessions.router, runs.router, datasets.router, observability.router):
        app.include_router(router, prefix=_V1)
    _publish_event_schema(app)
    return app


def _publish_event_schema(app: FastAPI) -> None:
    """The SSE bodies are ``RunEvent``s; FastAPI does not see them, so they join the document."""

    def openapi() -> dict[str, Any]:
        if app.openapi_schema is not None:
            return app.openapi_schema
        from fastapi.openapi.utils import get_openapi

        schema = get_openapi(title=app.title, version=app.version, routes=app.routes)
        event = RunEvent.model_json_schema(by_alias=True, ref_template="#/components/schemas/{model}")
        components = schema.setdefault("components", {}).setdefault("schemas", {})
        components.update(event.pop("$defs", {}))
        components["RunEvent"] = event
        app.openapi_schema = schema
        return schema

    app.openapi = openapi  # type: ignore[method-assign]


def app_factory() -> FastAPI:
    logging_policy.install()  # library records go to the run's trace, nowhere else (design 12)
    return create_app(AppDeps.from_environment(EnvironmentModel()))


def create_openapi() -> dict[str, Any]:
    """The OpenAPI document of the published contract (``generated/openapi.json``, case A-10)."""
    return create_app(resources=load_resources(EnvironmentModel(env="dev"))).openapi()
