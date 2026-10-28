"""Binds a node's input ports into its ``NodeInput`` (design 8.1 step 3, 8.2; case I-03, I-04).

Ported from the Node.js ``port-binder.ts``. For each port the first source that yields a
value wins: the configuration value (templates resolved), the type's default template, the
state path, then the port's default. Every value is validated against the port's schema
before the kind runs. Templates are resolved by the caller's ``resolve`` and ``read``
(``io`` sits below the template resolver, design 4.3).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from zonic_runtime.errors import ZonicError
from zonic_runtime.io.encoders import encoder
from zonic_runtime.io.schema_registry import SchemaRegistry
from zonic_runtime.models.io.node_io import NodeInput
from zonic_runtime.models.io.ports import NodeIoSpec
from zonic_runtime.runtime.paths import get_path

_CONFIG_PREFIX = "config."


def yields(value: Any) -> bool:
    """A source yields a value unless it is missing, ``None`` or a blank string."""
    return value is not None and not (isinstance(value, str) and not value.strip())


def _resolved(resolve: Callable[..., Any], value: Any, encode: str | None) -> Any:
    found = encoder(encode, value)
    return resolve(value) if found is None else resolve(value, encode=found)


class PortBinder:
    def __init__(self, schemas: SchemaRegistry) -> None:
        self._schemas = schemas

    def bind(
        self,
        node_id: str,
        io: NodeIoSpec | None,
        config: Mapping[str, Any],
        visit: int,
        resolve: Callable[..., Any],
        read: Callable[[str], Any],
    ) -> NodeInput:
        """``resolve(value, encode=...)`` turns a value with templates into data; ``read`` reads a
        state path. A binding's ``encode`` applies to its configuration value and template."""
        values: dict[str, Any] = {}
        for port in io.inputs if io is not None else []:
            binding = port.binding
            value: Any = None
            if binding.config_path is not None:
                path = binding.config_path.removeprefix(_CONFIG_PREFIX)
                value = _resolved(resolve, get_path(dict(config), path), binding.encode)
            if not yields(value) and binding.template is not None:
                value = _resolved(resolve, binding.template, binding.encode)
            if not yields(value) and binding.state_path is not None:
                value = read(binding.state_path)
            if not yields(value):
                value = port.default
            if not yields(value):
                if port.required:
                    raise ZonicError("INPUT_PORT_MISSING", node_id=node_id, port=port.name)
                continue
            issues = self._schemas.validate(
                self._schemas.schema(port.schema_ref), value, f"inputs.{port.name}"
            )
            if issues:
                raise ZonicError(
                    "INPUT_INVALID",
                    node_id=node_id,
                    port=port.name,
                    issues=[{"path": i.path, "message": i.message} for i in issues],
                )
            values[port.name] = value
        return NodeInput(node_id, visit, values)
