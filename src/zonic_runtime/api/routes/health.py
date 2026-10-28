"""``/healthz`` (liveness) and ``/readyz`` (the gateway answers); no token needed.

Ready is ``ok``; ``degraded`` while the process sheds new runs (pressure, case Q-15) still answers
``200``, since its running runs continue; ``not_ready`` (``503``) names the failing dependency.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from zonic_runtime.api.deps import deps_of
from zonic_runtime.models.api.responses import HealthStatus

router = APIRouter(tags=["health"])
_DEGRADED = "degraded"


@router.get(
    "/healthz",
    operation_id="getHealth",
    summary="Liveness",
    response_model=HealthStatus,
    response_model_exclude_none=True,
)
async def healthz(request: Request) -> HealthStatus:
    return HealthStatus(status="ok", release=deps_of(request).release)


@router.get(
    "/readyz",
    operation_id="getReadiness",
    summary="Readiness: the gateway and other dependencies answer",
    response_model=HealthStatus,
    response_model_exclude_none=True,
    responses={
        503: {"description": "Not ready; `checks` names the failing dependency", "model": HealthStatus}
    },
)
async def readyz(request: Request) -> JSONResponse:
    deps = deps_of(request)
    gateway = await deps.gateway.ready()
    checks = {"gateway": "ok" if gateway else "unavailable", **deps.ready_checks()}
    status = "not_ready" if not gateway else "degraded" if _DEGRADED in checks.values() else "ok"
    body = HealthStatus(status=status, release=deps.release, checks=checks)
    return JSONResponse(
        body.model_dump(mode="json", by_alias=True, exclude_none=True), status_code=200 if gateway else 503
    )
