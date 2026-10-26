"""SP-2: a minimal ``TurnSaver`` — ``InMemorySaver`` plus a JSON bundle export and import.

The bundle is what the runtime would store in ``runtime_checkpoint.body``:
``{format, langgraphVersion, threadId, checkpoints, writes, blobs}`` with every
serialized payload zstd-compressed and base64-encoded. ``keep_latest`` keeps only the
newest checkpoint of each namespace, its pending writes and the blobs it references.
"""

from __future__ import annotations

import base64
from importlib.metadata import version
from typing import Any

import zstandard
from langgraph.checkpoint.memory import InMemorySaver

FORMAT = "zonic-turn-v1"


def _pack(data: bytes) -> str:
    return base64.b64encode(zstandard.ZstdCompressor(level=3).compress(data)).decode()


def _unpack(text: str) -> bytes:
    return zstandard.ZstdDecompressor().decompress(base64.b64decode(text))


class TurnSaver(InMemorySaver):
    def export_bundle(self, thread_id: str, *, keep_latest: bool = True) -> dict[str, Any]:
        namespaces = self.storage.get(thread_id, {})
        checkpoints: list[list[Any]] = []
        kept: set[tuple[str, str]] = set()
        versions: set[tuple[str, str, Any]] = set()
        for ns, by_id in namespaces.items():
            ids = sorted(by_id, reverse=True)[:1] if keep_latest else list(by_id)
            for checkpoint_id in ids:
                (ctype, cbytes), (mtype, mbytes), parent = by_id[checkpoint_id]
                checkpoints.append([ns, checkpoint_id, ctype, _pack(cbytes), mtype, _pack(mbytes), parent])
                kept.add((ns, checkpoint_id))
                body = self.serde.loads_typed((ctype, cbytes))
                versions.update((ns, ch, v) for ch, v in body["channel_versions"].items())
        writes = [
            [ns, checkpoint_id, task_id, idx, channel, vtype, _pack(vbytes), task_path]
            for (tid, ns, checkpoint_id), items in self.writes.items()
            if tid == thread_id and (ns, checkpoint_id) in kept
            for (task_id, idx), (_, channel, (vtype, vbytes), task_path) in items.items()
        ]
        blobs = [
            [ns, channel, ver, btype, _pack(bbytes)]
            for (tid, ns, channel, ver), (btype, bbytes) in self.blobs.items()
            if tid == thread_id and (not keep_latest or (ns, channel, ver) in versions)
        ]
        return {
            "format": FORMAT,
            "langgraphVersion": version("langgraph"),
            "threadId": thread_id,
            "checkpoints": checkpoints,
            "writes": writes,
            "blobs": blobs,
        }

    @classmethod
    def from_bundle(cls, bundle: dict[str, Any]) -> TurnSaver:
        if bundle.get("format") != FORMAT:
            raise ValueError(f"unknown checkpoint format {bundle.get('format')!r}")
        saver = cls()
        thread_id = bundle["threadId"]
        for ns, checkpoint_id, ctype, cdata, mtype, mdata, parent in bundle["checkpoints"]:
            saver.storage[thread_id][ns][checkpoint_id] = (
                (ctype, _unpack(cdata)),
                (mtype, _unpack(mdata)),
                parent,
            )
        for ns, checkpoint_id, task_id, idx, channel, vtype, vdata, task_path in bundle["writes"]:
            saver.writes[(thread_id, ns, checkpoint_id)][(task_id, idx)] = (
                task_id,
                channel,
                (vtype, _unpack(vdata)),
                task_path,
            )
        for ns, channel, ver, btype, bdata in bundle["blobs"]:
            saver.blobs[(thread_id, ns, channel, ver)] = (btype, _unpack(bdata))
        return saver
