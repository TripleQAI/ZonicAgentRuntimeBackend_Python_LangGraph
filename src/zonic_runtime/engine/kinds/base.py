"""The node kind contract (design 8.1, 8.4; plan P1-10).

A kind implements only ``execute``: it reads its validated ``NodeInput`` and the run through
the ``NodeContext`` (never through state it keeps), and returns a ``NodeOutcome`` whose
``output`` is the raw result the type's ``output.map`` reads, and whose ``activate`` names
the handles to follow. Kind instances are built once per compiled graph and shared by every
run of it, so they must hold no run data (rule 14, case C-15).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar, Protocol, runtime_checkable

from zonic_runtime.models.engine import InterruptRequest, NodeOutcome
from zonic_runtime.models.io.node_io import NodeInput

if TYPE_CHECKING:
    from zonic_runtime.engine.node_template import NodeContext


@runtime_checkable
class NodeKind(Protocol):
    #: ``interrupt()`` before any side effect; the answer reaches ``execute`` (design 9.4).
    interrupts_first: ClassVar[bool]
    #: Holds a run-wide node permit while executing; container kinds release it (design 11.2).
    holds_permit: ClassVar[bool]

    async def execute(self, ctx: NodeContext, node_input: NodeInput) -> NodeOutcome: ...


class BaseKind:
    """Defaults for kinds: no interrupt, holds a permit, timed by its node's ``TimeoutPolicy``."""

    interrupts_first: ClassVar[bool] = False
    holds_permit: ClassVar[bool] = True
    #: The kind enforces the node's timeout itself (the Swarm's ``SWARM_TIMEOUT``): the compiler
    #: gives its node no run timeout, so the two never race.
    owns_deadline: ClassVar[bool] = False

    def request(self, ctx: NodeContext) -> InterruptRequest:
        """The interrupt payload of an ``interrupts_first`` kind."""
        raise NotImplementedError

    def answer_model(self, ctx: NodeContext) -> Any:
        """The answer's model, for ``interrupt(response_schema=...)``; ``None`` for any answer."""
        return None

    def response(self, ctx: NodeContext, answer: Any) -> Any:
        """What ``interrupt()`` returned, as the node's templates read it (``interrupt_answer``)."""
        return answer

    async def execute(self, ctx: NodeContext, node_input: NodeInput) -> NodeOutcome:
        raise NotImplementedError
