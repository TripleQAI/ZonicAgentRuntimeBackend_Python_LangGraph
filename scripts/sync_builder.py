"""Vendor the builders' workflow documents into ``tests/fixtures/workflows/builder/`` (plan 20.1, 20.7).

Sets, all from sibling repositories, never modified there:

* ``zonic/``: the 7 Zonic exports as the FastAPI builder wrote them (no ``schemaVersion``);
* ``zonic-versioned/``: the same 7 with an explicit ``schemaVersion`` (the builder's
  ``docs/examples-agent-wokflow``);
* ``templates/``: the 4 builder templates (``{name, title, description, document}``);
* ``invalid/``: ``new-workflow.json``, a Swarm with no members;
* ``platform/``: the InSite platform imports, ``agent.json`` and the 8 documents of
  ``sub_agents.json``, one file per document;
* ``django/``: the Django runtime workflows that add a case (``complex-agent``);
* ``schema/``: ``workflow-document-v2.schema.json``, for the model contract test.

``open-data/`` is written by ``scripts/export_workflows.py`` from the product database;
this script keeps its entries. ``SOURCE.json`` records each file's source and SHA-256 and
each repository's commit. ``--check`` fails when a vendored file no longer matches its
hash, when files and entries differ, or (when the repositories are next to this one)
when a source changed since it was vendored.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "tests" / "fixtures" / "workflows" / "builder"
MANIFEST = TARGET / "SOURCE.json"
BUILDER = "Strands-visual-builder"
DJANGO = "ZonicAgentRuntimeBackend_DjangoApp"
DJANGO_WORKFLOWS = "airuntimebackendmodule/code/aiplatform/tests_all/tests/test_data/workflows"
#: Sets not produced here; their entries are kept as they are.
EXTERNAL_SETS = ("open-data/",)


@dataclass(frozen=True)
class Vendored:
    path: str  # relative to TARGET
    repo: str
    source: str  # relative to the repository, with "#<index>" for one document of a list
    content: Callable[[], bytes]


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _dump(document: Any) -> bytes:
    return (json.dumps(document, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def _copy(repo_dir: Path, source: str) -> Callable[[], bytes]:
    return lambda: (repo_dir / source).read_bytes()


def _plan(parent: Path) -> Iterator[Vendored]:
    builder = parent / BUILDER
    for f in sorted((builder / "backend/tests/workflow/fixtures/zonic").glob("*.json")):
        src = f.relative_to(builder).as_posix()
        yield Vendored(f"zonic/{f.name}", BUILDER, src, _copy(builder, src))
    for f in sorted((builder / "docs/examples-agent-wokflow").glob("*.json")):
        src = f.relative_to(builder).as_posix()
        folder = "invalid" if f.name == "new-workflow.json" else "zonic-versioned"
        yield Vendored(f"{folder}/{f.name}", BUILDER, src, _copy(builder, src))
    for f in sorted((builder / "backend/workflow/resources/templates").glob("*.json")):
        src = f.relative_to(builder).as_posix()
        yield Vendored(f"templates/{f.name}", BUILDER, src, _copy(builder, src))
    schema = "backend/workflow/resources/schemas/workflow-document-v2.schema.json"
    yield Vendored("schema/workflow-document-v2.schema.json", BUILDER, schema, _copy(builder, schema))

    fixtures = builder / "backend/tests/workflow/fixtures"
    root_doc = json.loads((fixtures / "agent.json").read_text(encoding="utf-8"))
    yield Vendored(
        f"platform/{_slug(root_doc['name'])}.json",
        BUILDER,
        "backend/tests/workflow/fixtures/agent.json",
        lambda: _dump(json.loads((fixtures / "agent.json").read_text(encoding="utf-8"))),
    )
    subs = json.loads((fixtures / "sub_agents.json").read_text(encoding="utf-8"))
    for i, sub in enumerate(subs):
        yield Vendored(
            f"platform/{_slug(sub['name'])}.json",
            BUILDER,
            f"backend/tests/workflow/fixtures/sub_agents.json#{i}",
            lambda i=i: _dump(json.loads((fixtures / "sub_agents.json").read_text(encoding="utf-8"))[i]),
        )

    django = parent / DJANGO
    src = f"{DJANGO_WORKFLOWS}/complex-agent.json"
    yield Vendored("django/complex-agent.json", DJANGO, src, _copy(django, src))


def _commit(repo_dir: Path) -> str:
    done = subprocess.run(
        ["git", "-C", str(repo_dir), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    )
    return done.stdout.strip()


def _manifest() -> dict[str, Any]:
    if MANIFEST.is_file():
        data: dict[str, Any] = json.loads(MANIFEST.read_text(encoding="utf-8"))
        return data
    return {"repos": {}, "files": {}}


def sync(parent: Path) -> int:
    manifest = _manifest()
    kept = {p: e for p, e in manifest["files"].items() if p.startswith(EXTERNAL_SETS)}
    for old in TARGET.rglob("*.json"):
        rel = old.relative_to(TARGET).as_posix()
        if old != MANIFEST and not rel.startswith(EXTERNAL_SETS):
            old.unlink()
    files: dict[str, Any] = {}
    for item in _plan(parent):
        data = item.content()
        out = TARGET / item.path
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(data)
        files[item.path] = {"source": f"{item.repo}/{item.source}", "sha256": _sha256(data)}
    files.update(kept)
    repos = {
        **manifest.get("repos", {}),
        BUILDER: _commit(parent / BUILDER),
        DJANGO: _commit(parent / DJANGO),
    }
    MANIFEST.write_text(
        json.dumps({"repos": repos, "files": dict(sorted(files.items()))}, indent=2) + "\n", encoding="utf-8"
    )
    print(f"vendored {len(files) - len(kept)} files ({len(kept)} kept from {', '.join(EXTERNAL_SETS)})")
    return 0


def check(parent: Path) -> int:
    manifest = _manifest()
    entries: dict[str, Any] = manifest["files"]
    present = {p.relative_to(TARGET).as_posix() for p in TARGET.rglob("*.json") if p != MANIFEST}
    problems = [f"{p}: not in SOURCE.json" for p in sorted(present - set(entries))]
    problems += [f"{p}: listed in SOURCE.json but missing" for p in sorted(set(entries) - present)]
    for path in sorted(present & set(entries)):
        if _sha256((TARGET / path).read_bytes()) != entries[path]["sha256"]:
            problems.append(f"{path}: does not match SOURCE.json")
    if (parent / BUILDER).is_dir() and (parent / DJANGO).is_dir():
        for item in _plan(parent):
            entry = entries.get(item.path)
            if entry and _sha256(item.content()) != entry["sha256"]:
                problems.append(f"{item.path}: {item.repo}/{item.source} changed since it was vendored")
    for problem in problems:
        print(problem)
    if problems:
        print("run: uv run python -m scripts.sync_builder")
        return 1
    print(f"tests/fixtures/workflows/builder is current ({len(entries)} files)")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="fail when the vendored files are not current")
    parser.add_argument(
        "--repos", type=Path, default=ROOT.parent, help="folder holding the sibling repositories"
    )
    args = parser.parse_args(argv)
    return check(args.repos) if args.check else sync(args.repos)


if __name__ == "__main__":
    sys.exit(main())
