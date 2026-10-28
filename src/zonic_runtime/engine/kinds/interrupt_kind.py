"""The ``interrupt`` kind (node types ``approval`` and ``human_input``; design 9.4; plan P4-02;
cases N-20, M-10, H-11).

The node template calls ``interrupt(request, response_schema=AnswerModel)`` before anything else
(step 2), so a visit that pauses has no side effect (DR-10): ``request`` binds the prompt port only
(``message`` or ``prompt``, templates resolved) and gives the form (the manifest's ``options.form``),
the answer's JSON Schema and the expiry (``timeoutHours``, else
``settings.interrupts.defaultExpiryHours``). On the resumed pass ``interrupt()`` returns the answer,
validated by ``AnswerModel``:

* a form with ``yes`` and ``no`` handles (Approval) decides: ``{approved, comment, decidedBy}``,
  following ``approved`` or ``rejected``;
* any other form (Human Input) collects ``value``, typed by ``inputType`` through the manifest's
  ``options.valueSchemas`` and required unless ``required`` is false.

The response is ``{{approval.response}}`` / ``{{human.response}}`` (the decision, or the value) in
the node's templates, and is written to ``responseVariable`` when that names a state path.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import timedelta
from types import MappingProxyType
from typing import Any, ClassVar

from pydantic import BaseModel, Field, create_model

from zonic_runtime.engine.approvals import answer_value
from zonic_runtime.engine.kinds.base import BaseKind
from zonic_runtime.engine.node_template import NodeContext
from zonic_runtime.errors import ZonicError
from zonic_runtime.models.api.answers import ApprovalAnswer
from zonic_runtime.models.base import ZonicModel
from zonic_runtime.models.blueprint import Blueprint, NodeSpec
from zonic_runtime.models.engine import InterruptRequest, NodeOutcome, StateUpdate
from zonic_runtime.models.io.node_io import NodeInput
from zonic_runtime.models.nodes.configs import InterruptNodeConfig

YES, NO = "yes", "no"
_TEXT = "text"
#: JSON Schema types of ``options.valueSchemas`` -> Python types for the answer model.
_TYPES: Mapping[str, Any] = MappingProxyType(
    {"string": str, "number": float, "integer": int, "boolean": bool}
)


class InterruptKind(BaseKind):
    interrupts_first: ClassVar[bool] = True

    def __init__(self, spec: NodeSpec, blueprint: Blueprint) -> None:
        self.spec, self.blueprint = spec, blueprint
        config = spec.config
        assert isinstance(config, InterruptNodeConfig)
        self.config = config
        self._model: type[BaseModel] | None = None

    @staticmethod
    def handles(ctx: NodeContext) -> Mapping[str, str]:
        entry = ctx.services.resources.manifest.node_types.get(ctx.spec.node_type)
        return entry.handles if entry is not None else {}

    def decides(self, ctx: NodeContext) -> bool:
        handles = self.handles(ctx)
        return YES in handles and NO in handles

    def options(self, ctx: NodeContext) -> Mapping[str, Any]:
        entry = ctx.services.resources.manifest.node_types.get(self.spec.node_type)
        return entry.options if entry is not None else {}

    def answer_model(self, ctx: NodeContext) -> type[BaseModel]:
        """The answer's model, from the node's configuration (case M-10); built once per spec."""
        if self._model is not None:
            return self._model
        if self.decides(ctx):
            self._model = ApprovalAnswer
            return self._model
        options = self.options(ctx)
        input_type = self.config.input_type or str(options.get("defaultInputType") or _TEXT)
        schema = (options.get("valueSchemas") or {}).get(input_type)
        if not isinstance(schema, Mapping):
            raise ZonicError("DOCUMENT_INVALID", node_id=self.spec.node_id, input_type=input_type)
        python = _TYPES.get(str(schema.get("type")), Any)
        description = schema.get("description")
        if self.config.required is False:
            field: Any = (python | None, Field(default=None, description=description))
        else:
            field = (python, Field(description=description))
        self._model = create_model("InputAnswer", __base__=ZonicModel, value=field)
        return self._model

    def request(self, ctx: NodeContext) -> InterruptRequest:
        """What the node asks; binding the prompt port has no side effect (DR-10)."""
        config = self.spec.config.model_dump(by_alias=True, exclude_none=True)
        bound = ctx.services.binder.bind(
            self.spec.node_id, self.spec.io, config, ctx.visit, ctx.resolve, ctx.read
        )
        prompt = next((v for v in bound.all().values() if v is not None), None)
        hours = self.config.timeout_hours or ctx.services.settings.interrupts.default_expiry_hours
        return InterruptRequest(
            kind=str(self.options(ctx).get("form") or self.spec.node_type),
            node_id=self.spec.node_id,
            prompt={_TEXT: prompt if isinstance(prompt, str) else "" if prompt is None else str(prompt)},
            answer_schema=self.answer_model(ctx).model_json_schema(by_alias=True),
            ui_schema={},
            expires_at=ctx.started_at + timedelta(hours=hours),
        )

    def response(self, ctx: NodeContext, answer: Any) -> Any:
        """``{{approval.response}}`` / ``{{human.response}}``: the decision, or the collected value."""
        found = answer_value(answer)
        if self.decides(ctx) or not isinstance(found, Mapping):
            return found
        return found.get("value")

    async def execute(self, ctx: NodeContext, node_input: NodeInput) -> NodeOutcome:
        response = ctx.interrupt_answer
        updates = []
        target = self.config.response_variable
        if target and target not in ctx.services.resources.templates.special:  # not the answer's own alias
            updates.append(StateUpdate(path=target, op="set", value=response))
        if self.decides(ctx):
            decision = dict(response) if isinstance(response, Mapping) else {}
            approved = decision.get("approved") is True
            handles = self.handles(ctx)
            output = {
                "approved": approved,
                "comment": decision.get("comment"),
                "decidedBy": decision.get("decidedBy"),
            }
            return NodeOutcome(
                output={k: v for k, v in output.items() if v is not None},
                activate=[handles[YES] if approved else handles[NO]],
                updates=updates,
            )
        return NodeOutcome(
            output={"value": response},
            activate=[ctx.services.resources.manifest.defaults.default_handle],
            updates=updates,
        )
