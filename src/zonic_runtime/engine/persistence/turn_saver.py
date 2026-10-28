"""``TurnSaver``: the checkpointer of root graphs (design 9.2; plan P2-04, SP-2, cases P-05 to P-09).

An ``InMemorySaver`` with one buffer per thread (the run ID), keyed by checkpoint namespace so child
and agent graphs keep their own checkpoints. It never calls the gateway: the run attaches a buffer
before invoking (empty for a new turn, restored from a ``CheckpointBundle`` for a resume) and
detaches it afterwards, which hands the latest ``state.checkpointsKeptPerNamespace`` checkpoints
per namespace, their pending writes and the blobs they reference to the save, and frees the
thread. Writes to a thread that is not attached are ``CHECKPOINT_THREAD_UNKNOWN`` (``strict``; the
conformance suite runs with it off).

Serialization is ``JsonPlusSerializer`` (no pickle fallback), optionally ``EncryptedSerializer``
with an AES key (settings ``state.encryption``, Q10).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import ChannelVersions, Checkpoint, CheckpointMetadata
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.serde.base import SerializerProtocol
from langgraph.checkpoint.serde.encrypted import EncryptedSerializer
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

from zonic_runtime.engine.persistence.codec import CheckpointBundle, pack, unpack
from zonic_runtime.errors import ZonicError

UNKNOWN_THREAD = "CHECKPOINT_THREAD_UNKNOWN"
_CONFIGURABLE, _THREAD = "configurable", "thread_id"


def serializer(aes_key: bytes | None = None) -> SerializerProtocol:
    plain = JsonPlusSerializer(pickle_fallback=False)
    if aes_key is None:
        return plain
    return EncryptedSerializer.from_pycryptodome_aes(serde=plain, key=aes_key)


class TurnSaver(InMemorySaver):
    def __init__(
        self, *, serde: SerializerProtocol | None = None, keep: int = 2, strict: bool = True
    ) -> None:
        super().__init__(serde=serde or serializer())
        self._keep = max(1, keep)
        self._strict = strict
        self._attached: set[str] = set()
        #: ``aput`` calls per thread, for tests of the durability modes (case P-06).
        self.puts: dict[str, int] = {}

    # -- attach and detach ---------------------------------------------------------------------

    def attach(self, thread_id: str, bundle: CheckpointBundle | None = None) -> None:
        self._drop(thread_id)
        self._attached.add(thread_id)
        if bundle is None:
            return
        for ns, checkpoint_id, ctype, cdata, mtype, mdata, parent in bundle.checkpoints:
            self.storage[thread_id][ns][checkpoint_id] = (
                (ctype, unpack(cdata)),
                (mtype, unpack(mdata)),
                parent,
            )
        for ns, checkpoint_id, task_id, index, channel, vtype, vdata, task_path in bundle.writes:
            self.writes[(thread_id, ns, checkpoint_id)][(task_id, index)] = (
                task_id,
                channel,
                (vtype, unpack(vdata)),
                task_path,
            )
        for ns, channel, ver, btype, bdata in bundle.blobs:
            self.blobs[(thread_id, ns, channel, ver)] = (btype, unpack(bdata))

    def detach(self, thread_id: str) -> CheckpointBundle:
        bundle = self.export(thread_id)
        self._drop(thread_id)
        return bundle

    def attached(self, thread_id: str) -> bool:
        return thread_id in self._attached

    def export(self, thread_id: str) -> CheckpointBundle:
        """The latest ``keep`` checkpoints per namespace, their pending writes and blobs."""
        checkpoints: list[list[Any]] = []
        kept: set[tuple[str, str]] = set()
        versions: set[tuple[str, str, Any]] = set()
        for ns, by_id in self.storage.get(thread_id, {}).items():
            for checkpoint_id in sorted(by_id, reverse=True)[: self._keep]:
                (ctype, cbytes), (mtype, mbytes), parent = by_id[checkpoint_id]
                checkpoints.append([ns, checkpoint_id, ctype, pack(cbytes), mtype, pack(mbytes), parent])
                kept.add((ns, checkpoint_id))
                body = self.serde.loads_typed((ctype, cbytes))
                versions.update((ns, channel, v) for channel, v in body["channel_versions"].items())
        writes = [
            [ns, checkpoint_id, task_id, index, channel, vtype, pack(vbytes), task_path]
            for (tid, ns, checkpoint_id), items in self.writes.items()
            if tid == thread_id and (ns, checkpoint_id) in kept
            for (task_id, index), (_, channel, (vtype, vbytes), task_path) in items.items()
        ]
        blobs = [
            [ns, channel, ver, btype, pack(bbytes)]
            for (tid, ns, channel, ver), (btype, bbytes) in self.blobs.items()
            if tid == thread_id and (ns, channel, ver) in versions
        ]
        return CheckpointBundle(thread_id=thread_id, checkpoints=checkpoints, writes=writes, blobs=blobs)

    def _drop(self, thread_id: str) -> None:
        self._attached.discard(thread_id)
        self.puts.pop(thread_id, None)
        super().delete_thread(thread_id)

    def _check(self, config: RunnableConfig) -> str:
        configurable: Any = config.get(_CONFIGURABLE) or {}
        thread_id = str(configurable.get(_THREAD, ""))
        if self._strict and thread_id not in self._attached:
            raise ZonicError(UNKNOWN_THREAD, thread_id=thread_id)
        return thread_id

    # -- BaseCheckpointSaver --------------------------------------------------------------------

    def put(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        thread_id = self._check(config)
        self.puts[thread_id] = self.puts.get(thread_id, 0) + 1
        return super().put(config, checkpoint, metadata, new_versions)

    def put_writes(
        self, config: RunnableConfig, writes: Sequence[tuple[str, Any]], task_id: str, task_path: str = ""
    ) -> None:
        self._check(config)
        super().put_writes(config, writes, task_id, task_path)

    async def aput(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        return self.put(config, checkpoint, metadata, new_versions)

    async def aput_writes(
        self, config: RunnableConfig, writes: Sequence[tuple[str, Any]], task_id: str, task_path: str = ""
    ) -> None:
        self.put_writes(config, writes, task_id, task_path)

    def delete_thread(self, thread_id: str) -> None:
        self._drop(thread_id)

    async def adelete_thread(self, thread_id: str) -> None:
        self._drop(thread_id)

    def threads(self) -> Iterable[str]:
        return list(self.storage)
