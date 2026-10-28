"""``GET /v1/catalog/agents`` and ``GET /v1/meta/node-types``."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request

from zonic_runtime.api.deps import current_caller, deps_of
from zonic_runtime.api.errors import ERROR_RESPONSES
from zonic_runtime.application.caller import Caller
from zonic_runtime.models.api.responses import AgentCardList, NodeTypeList

router = APIRouter(responses=ERROR_RESPONSES)
CallerDep = Annotated[Caller, Depends(current_caller)]


@router.get(
    "/catalog/agents",
    operation_id="listAgents",
    summary="Workflows the caller may run",
    tags=["catalog"],
    response_model=AgentCardList,
    response_model_exclude_none=True,
)
async def list_agents(request: Request, caller: CallerDep) -> AgentCardList:
    return await deps_of(request).catalog.agents(caller)


@router.get(
    "/meta/node-types",
    operation_id="listNodeTypes",
    summary="Node types the runtime executes, with input ports and output schemas",
    tags=["meta"],
    response_model=NodeTypeList,
    response_model_exclude_none=True,
)
async def list_node_types(request: Request, caller: CallerDep) -> NodeTypeList:
    return deps_of(request).catalog.node_types()
