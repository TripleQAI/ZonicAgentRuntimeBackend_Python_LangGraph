"""The ``entry``, ``router``, ``variable``, ``output`` and ``ignore`` kinds (design 8.4; plan P2-02).

* ``entry``: the run's query and the workflow's interface inputs (a sub-flow's input values,
  else their defaults). The output also holds every given input under ``interface.inputs``,
  which the platform documents' start nodes read as ``{{nodeOutput.interface.inputs.<name>}}``.
* ``router``: the first enabled branch whose conditions hold activates its ``ruleId``; then the
  default branch; with neither, ``ROUTE_UNRESOLVED`` and the node's error policy.
* ``variable``: its ``updates``, applied by the template's update step with the builder's state
  operations; the output is the ``flow`` keys they change (removed keys as ``null``).
* ``output``: the run result from the ``mapping`` port, each value converted to its mapping
  type. Without a mapping it sets no result, warns ``OUTPUT_MAPPING_MISSING``, and the result
  fallback applies (case D-13).
* ``ignore``: notes; never compiled, so this kind only follows ``default`` if ever run.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from zonic_runtime.engine.kinds.base import BaseKind
from zonic_runtime.engine.node_template import NodeContext
from zonic_runtime.errors import ZonicError
from zonic_runtime.models.blueprint import Blueprint, NodeSpec
from zonic_runtime.models.engine import NodeOutcome, StateUpdate
from zonic_runtime.models.io.node_io import NodeInput
from zonic_runtime.workflow.templates.conditions import select_branch
from zonic_runtime.workflow.templates.updates import convert_mapped

MAPPING_MISSING = "OUTPUT_MAPPING_MISSING"


class _Kind(BaseKind):
    def __init__(self, spec: NodeSpec, blueprint: Blueprint) -> None:
        self.spec, self.blueprint = spec, blueprint

    @staticmethod
    def default(ctx: NodeContext) -> list[str]:
        return [ctx.services.resources.manifest.defaults.default_handle]


class EntryKind(_Kind):
    async def execute(self, ctx: NodeContext, node_input: NodeInput) -> NodeOutcome:
        given = ctx.state.get("input") or {}
        values: dict[str, Any] = {}
        declared = node_input.get("interfaceInputs")
        for item in declared if isinstance(declared, list) else []:
            if isinstance(item, Mapping) and isinstance(item.get("name"), str):
                name = item["name"]
                values[name] = given[name] if name in given else item.get("default")
        output: dict[str, Any] = {**values, "interface": {"inputs": {**values, **given}}}
        query = node_input.get("query")
        if query is not None:
            output["query"] = query
        return NodeOutcome(output=output, activate=self.default(ctx))


class RouterKind(_Kind):
    async def execute(self, ctx: NodeContext, node_input: NodeInput) -> NodeOutcome:
        choice = select_branch(
            self.spec.config.rules,  # type: ignore[attr-defined]
            ctx.services.resources.dialects.default_rule_ids,
            ctx.services.evaluator,
            ctx.resolve,
            self.blueprint.edge_index.handles(self.spec.node_id),
        )
        if choice.handle is None:
            raise ZonicError("ROUTE_UNRESOLVED", node_id=self.spec.node_id, reason="no branch matched")
        output = {"handle": choice.handle, "matchedRule": choice.matched}
        return NodeOutcome(
            output={k: v for k, v in output.items() if v is not None}, activate=[choice.handle]
        )


class VariableKind(_Kind):
    async def execute(self, ctx: NodeContext, node_input: NodeInput) -> NodeOutcome:
        updates = [StateUpdate.model_validate(u) for u in getattr(self.spec.config, "updates", None) or []]
        written = ctx.services.writer.apply(updates, ctx.template_context())
        changed = {**written.flow, **dict.fromkeys(written.flow_removed)}
        return NodeOutcome(output={"changed": changed}, activate=self.default(ctx), updates=updates)


class OutputKind(_Kind):
    async def execute(self, ctx: NodeContext, node_input: NodeInput) -> NodeOutcome:
        mapping = node_input.get("mapping")
        entries = mapping if isinstance(mapping, list) else []
        result = {
            e["target"]: convert_mapped(
                e.get("value"), e.get("type") if isinstance(e.get("type"), str) else None
            )
            for e in entries
            if isinstance(e, Mapping) and isinstance(e.get("target"), str)
        }
        if not result:
            entry = ctx.services.resources.errors.errors.get(MAPPING_MISSING)
            warning = {
                "code": MAPPING_MISSING,
                "nodeId": self.spec.node_id,
                "message": entry.message if entry else "",
            }
            return NodeOutcome(output={"result": {}}, activate=self.default(ctx), warnings=[warning])
        return NodeOutcome(output={"result": result}, activate=self.default(ctx), result=result)


class IgnoreKind(_Kind):
    async def execute(self, ctx: NodeContext, node_input: NodeInput) -> NodeOutcome:
        return NodeOutcome(activate=self.default(ctx))
