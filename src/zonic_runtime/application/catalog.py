"""The catalog and the node-type metadata (design 14.1; plan P2-03, cases A-01, I-12)."""

from __future__ import annotations

from zonic_runtime.application.caller import Caller
from zonic_runtime.application.runtime import WorkflowRuntime
from zonic_runtime.io.specs import describe_node_types
from zonic_runtime.models.api.responses import AgentCard, AgentCardList, NodeTypeInfo, NodeTypeList


class CatalogService:
    def __init__(self, runtime: WorkflowRuntime) -> None:
        self._runtime = runtime

    async def agents(self, caller: Caller) -> AgentCardList:
        rows = await self._runtime.loader.catalog(caller.workflows())
        return AgentCardList(
            items=[
                AgentCard(
                    agentic_workflow_id=r.agentic_workflow_id,
                    name=r.name or "",
                    display_name=r.display_name,
                    description=r.description,
                    bpc=r.bpc,
                    updated_at=r.updated_at,
                )
                for r in rows
            ]
        )

    def node_types(self) -> NodeTypeList:
        described = describe_node_types(self._runtime.resources.manifest, self._runtime.services.schemas)
        return NodeTypeList(items=[NodeTypeInfo.model_validate(d) for d in described])
