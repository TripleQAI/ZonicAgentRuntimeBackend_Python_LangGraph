"""Vendor SQL scripts owned by sibling repositories, byte for byte, with a ``SOURCE.json``.

Three sets:

* ``runtime``: the ``agent_runtime`` tables, functions, indexes and procedures from
  ``ZonicAgentRuntimeServer_Nodejs/db/runtime`` into ``db/runtime/``. Both runtimes use
  one ``agent_runtime`` schema; ``004_views.sql``, ``006_grants.sql``, ``run_all.sql``
  and ``999_drop_all.sql`` stay this project's own.
* ``observability``: ``agent_observability`` 001 to 005 from
  ``ZonicAgentRuntimeServer_Nodejs/db/observability`` into ``db/observability/``;
  ``006_grants.sql``, ``run_all.sql`` and ``999_drop_all.sql`` stay this project's own.
* ``builder``: the ``agent_builder`` DDL from ``ZonicAgentBuilderServer_FastAPI/db`` into
  ``tests/fixtures/builder_db/``; the SQL tests install it first, inside the
  rolled-back transaction.

``SOURCE.json`` records the commit and SHA-256 of each file; source repositories are
never modified. ``--check`` fails when a vendored file no longer matches ``SOURCE.json``,
and, when the source repository is next to this one, when it has changed since.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class VendoredSet:
    name: str
    repo: str
    source: str
    target: Path
    files: tuple[str, ...]

    @property
    def repo_dir(self) -> Path:
        return ROOT.parent / self.repo

    @property
    def source_dir(self) -> Path:
        return self.repo_dir / self.source

    @property
    def manifest(self) -> Path:
        return self.target / "SOURCE.json"


SETS = (
    VendoredSet(
        name="runtime",
        repo="ZonicAgentRuntimeServer_Nodejs",
        source="db/runtime",
        target=ROOT / "db" / "runtime",
        files=("001_schema.sql", "002_functions_triggers.sql", "003_indexes.sql", "005_procedures.sql"),
    ),
    VendoredSet(
        name="observability",
        repo="ZonicAgentRuntimeServer_Nodejs",
        source="db/observability",
        target=ROOT / "db" / "observability",
        files=(
            "001_schema_create.sql",
            "002_functions.sql",
            "003_indexes.sql",
            "004_views.sql",
            "005_procedures.sql",
        ),
    ),
    VendoredSet(
        name="builder",
        repo="ZonicAgentBuilderServer_FastAPI",
        source="db",
        target=ROOT / "tests" / "fixtures" / "builder_db",
        # The builder's install order up to its read surface; 010 (procedures) and
        # 011 (grants) are not needed to read workflows.
        files=(
            "001_extensions.sql",
            "002_schema.sql",
            "003_functions_triggers.sql",
            "004_indexes.sql",
            "005_adapt_existing_schema.sql",
            "009_api_views.sql",
        ),
    ),
)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _commit(repo: Path) -> str:
    done = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    )
    return done.stdout.strip()


def check(vs: VendoredSet) -> list[str]:
    if not vs.manifest.is_file():
        return [f"{vs.name}: {vs.manifest.relative_to(ROOT).as_posix()} is missing"]
    manifest = json.loads(vs.manifest.read_text(encoding="utf-8"))
    problems = [
        f"{vs.name}: vendored {name} does not match SOURCE.json"
        for name, digest in manifest["files"].items()
        if not (vs.target / name).is_file() or _sha256((vs.target / name).read_bytes()) != digest
    ]
    if list(manifest["files"]) != list(vs.files):
        problems.append(f"{vs.name}: SOURCE.json lists different files than scripts/sync_sql.py")
    if vs.source_dir.is_dir():
        problems += [
            f"{vs.name}: {vs.repo}/{vs.source}/{name} changed since commit {manifest['commit'][:12]}"
            for name, digest in manifest["files"].items()
            if _sha256((vs.source_dir / name).read_bytes()) != digest
        ]
    return problems


def sync(vs: VendoredSet) -> str:
    vs.target.mkdir(parents=True, exist_ok=True)
    digests: dict[str, str] = {}
    for name in vs.files:
        data = (vs.source_dir / name).read_bytes()
        (vs.target / name).write_bytes(data)
        digests[name] = _sha256(data)
    manifest = {"source": f"{vs.repo}/{vs.source}", "commit": _commit(vs.repo_dir), "files": digests}
    vs.manifest.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest["commit"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="fail when the vendored files are not current")
    parser.add_argument("sets", nargs="*", choices=[vs.name for vs in SETS], help="default: all")
    args = parser.parse_args(argv)
    chosen = [vs for vs in SETS if not args.sets or vs.name in args.sets]

    if args.check:
        problems = [p for vs in chosen for p in check(vs)]
        for problem in problems:
            print(problem)
        if problems:
            print("run: uv run python -m scripts.sync_sql")
            return 1
        for vs in chosen:
            print(
                f"{vs.target.relative_to(ROOT).as_posix()} is current ({len(vs.files)} files from {vs.repo})"
            )
        return 0

    for vs in chosen:
        commit = sync(vs)
        print(f"{vs.name}: vendored {len(vs.files)} files from {vs.repo} at {commit[:12]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
