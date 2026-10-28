"""Rows of the views the workflow loader reads (design 5.1; plan P1-04).

``CatalogAgentRow`` is the runtime's catalog (``agent_runtime.v_agent_catalog``,
``db/runtime/004_views.sql``): one row per runnable workflow and publish channel.
``AgentExportRow`` and ``AgentRevisionRow`` are the builder's views
(``agent_builder.v_agent_export`` and ``v_agent_summary``, the builder's
``009_api_views.sql``): the stored document, and the ``updated_at`` that keys the
document cache for sub-flows, which are not catalog entries.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import Field

from zonic_runtime.models.base import ZonicModel
from zonic_runtime.models.gateway_meta import gateway_view


@gateway_view(schema="runtime", name="v_agent_catalog")
class CatalogAgentRow(ZonicModel):
    agentic_workflow_id: str
    name: str | None = None
    display_name: str | None = None
    description: str | None = None
    bpc: str | None = None
    environment: str | None = None
    updated_at: datetime
    publish_channels: list[Any] = Field(default_factory=list)
    node_count: int = Field(default=0, ge=0)
    #: One publish channel code; NULL for a workflow without channels.
    runtime_channel: str | None = None


@gateway_view(schema="builder", name="v_agent_export")
class AgentExportRow(ZonicModel):
    agentic_workflow_id: str
    parent_package_id: str | None = None
    is_root: bool = True
    export_json: dict[str, Any]
    owner_sub: str | None = None


@gateway_view(schema="builder", name="v_agent_summary")
class AgentRevisionRow(ZonicModel):
    agentic_workflow_id: str
    updated_at: datetime
