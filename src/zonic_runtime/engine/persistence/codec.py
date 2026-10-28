"""``CheckpointBundle``: a run's checkpoints as JSON for ``runtime_checkpoint.body`` (design 9.2;
plan P2-04, case P-09).

The serializer's payloads are already ``(type, bytes)``; each is zstd-compressed and
base64-encoded, because gateway arguments are JSON text. ``format`` and ``langgraphVersion`` are
recorded so a resume knows what wrote the bundle (SP-2).
"""

from __future__ import annotations

import base64
from importlib.metadata import version
from typing import Any

import zstandard
from pydantic import Field

from zonic_runtime.errors import ZonicError
from zonic_runtime.models.base import ZonicModel

FORMAT = "langgraph-jsonplus-zstd-v1"
_LEVEL = 3


def pack(data: bytes) -> str:
    return base64.b64encode(zstandard.ZstdCompressor(level=_LEVEL).compress(data)).decode("ascii")


def unpack(text: str) -> bytes:
    return zstandard.ZstdDecompressor().decompress(base64.b64decode(text))


def langgraph_version() -> str:
    return version("langgraph")


class CheckpointBundle(ZonicModel):
    format: str = FORMAT
    langgraph_version: str = Field(default_factory=langgraph_version)
    thread_id: str
    #: ``[ns, checkpointId, type, data, metadataType, metadata, parentId]``
    checkpoints: list[list[Any]] = Field(default_factory=list)
    #: ``[ns, checkpointId, taskId, index, channel, type, data, taskPath]``
    writes: list[list[Any]] = Field(default_factory=list)
    #: ``[ns, channel, version, type, data]``
    blobs: list[list[Any]] = Field(default_factory=list)

    @classmethod
    def read(cls, body: dict[str, Any]) -> CheckpointBundle:
        bundle = cls.model_validate(body)
        if bundle.format != FORMAT:
            raise ZonicError("CHECKPOINT_INVALID", format=bundle.format)
        return bundle

    @property
    def namespaces(self) -> list[str]:
        return sorted({str(c[0]) for c in self.checkpoints})
