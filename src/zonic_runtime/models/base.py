"""Base classes for every schema in the runtime (design Section 15.1).

Three families differ only in how they treat unknown fields:

* ``ZonicModel``: API requests and responses, events, records. Unknown fields
  are rejected.
* ``ZonicResourceModel``: ``resources/*.json`` files. Unknown fields are
  rejected and instances are immutable after start-up.
* ``ZonicDocumentModel``: builder documents. Unknown fields are kept, because
  the builder's round trip must preserve them, and are reported by the loader.

All use camelCase aliases on the wire and accept snake_case in Python.
"""

from __future__ import annotations

from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel


class ZonicModel(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        extra="forbid",
        validate_default=True,
        ser_json_timedelta="iso8601",
    )

    def wire(self) -> dict[str, Any]:
        """JSON-compatible dict with camelCase keys, as sent on the wire."""
        return self.model_dump(mode="json", by_alias=True, exclude_none=True)


class ZonicResourceModel(ZonicModel):
    model_config = ConfigDict(frozen=True)


class ZonicDocumentModel(ZonicModel):
    model_config = ConfigDict(extra="allow")

    #: Field names the loader does not report as unknown even when absent from the model.
    known_extra_keys: ClassVar[frozenset[str]] = frozenset()

    def unknown_keys(self) -> list[str]:
        extra = self.model_extra or {}
        return sorted(k for k in extra if k not in self.known_extra_keys)
