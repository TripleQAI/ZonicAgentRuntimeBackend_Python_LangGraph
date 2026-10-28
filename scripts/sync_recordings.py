"""Vendor the recorded upstream answers from the Node.js runtime (plan P3-12, decision 0006).

Two sets from ``ZonicAgentRuntimeServer_Nodejs``, never modified there:

* ``recordings``: every file of ``tests/fixtures/recordings`` (``contexts.json`` and one folder per
  workflow), which ``npm run record:fixtures`` writes, into ``tests/fixtures/recordings/``;
* ``documents``: the workflow documents those recordings were made against (each tool's
  recording is keyed by its request template), into ``tests/fixtures/recordings/documents/``.

``SOURCE.json`` records the commit and SHA-256 of each file. ``--check`` fails when a vendored
file no longer matches it, and, when the Node.js repository is next to this one, when the
source changed or gained or lost files since.

    uv run python -m scripts.sync_recordings [--check]
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path

from scripts.sync_sql import ROOT, _commit, _sha256

REPO = ROOT.parent / "ZonicAgentRuntimeServer_Nodejs"
TARGET = ROOT / "tests" / "fixtures" / "recordings"
MANIFEST = TARGET / "SOURCE.json"


@dataclass(frozen=True)
class RecordedSet:
    name: str
    source: str
    target: Path
    #: Fixed files; ``None``: every file under ``source``.
    files: tuple[str, ...] | None = None

    def listed(self) -> list[str]:
        if self.files is not None:
            return list(self.files)
        base = REPO / self.source
        return sorted(p.relative_to(base).as_posix() for p in base.rglob("*") if p.is_file())


SETS = (
    RecordedSet("recordings", "tests/fixtures/recordings", TARGET),
    RecordedSet(
        "documents",
        "tests/fixtures/workflows/builder",
        TARGET / "documents",
        (
            "open-data/fsh-energy-agent.json",
            "open-data/fsh-facility-agent.json",
            "open-data/fsh-risk-agent.json",
            "templates/facility_operations_swarm.json",
        ),
    ),
)


def check() -> list[str]:
    if not MANIFEST.is_file():
        return [f"{MANIFEST.relative_to(ROOT).as_posix()} is missing"]
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    problems: list[str] = []
    for item in SETS:
        recorded: dict[str, str] = manifest["sets"].get(item.name, {})
        problems += [
            f"{item.name}: vendored {name} does not match SOURCE.json"
            for name, digest in recorded.items()
            if not (item.target / name).is_file() or _sha256((item.target / name).read_bytes()) != digest
        ]
        if (REPO / item.source).is_dir():
            listed = item.listed()
            if sorted(recorded) != listed:
                problems.append(f"{item.name}: the Node.js repository has different files than SOURCE.json")
            problems += [
                f"{item.name}: {item.source}/{name} changed since commit {manifest['commit'][:12]}"
                for name, digest in recorded.items()
                if name in listed and _sha256((REPO / item.source / name).read_bytes()) != digest
            ]
    return problems


def sync() -> str:
    sets: dict[str, dict[str, str]] = {}
    for item in SETS:
        digests: dict[str, str] = {}
        for name in item.listed():
            data = (REPO / item.source / name).read_bytes()
            target = item.target / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            digests[name] = _sha256(data)
        sets[item.name] = digests
    commit = _commit(REPO)
    manifest = {"source": "ZonicAgentRuntimeServer_Nodejs", "commit": commit, "sets": sets}
    MANIFEST.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return commit


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="fail when the vendored files are not current")
    args = parser.parse_args(argv)
    if args.check:
        problems = check()
        for problem in problems:
            print(problem)
        if problems:
            print("run: uv run python -m scripts.sync_recordings")
            return 1
        print(f"{TARGET.relative_to(ROOT).as_posix()} is current")
        return 0
    commit = sync()
    print(f"vendored the recordings from ZonicAgentRuntimeServer_Nodejs at {commit[:12]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
