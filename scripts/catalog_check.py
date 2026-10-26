"""Fail when a catalog case due by now has no test, or a test names an unknown case.

A case is due once every task that proves it is listed in ``tests/progress.yaml``.
Tests reference a case by putting its ID without the dash in the test name, for
example ``test_E03_exclusive_branches_join_once`` proves E-03.
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

import yaml

from scripts import build_catalog

ROOT = Path(__file__).resolve().parents[1]
TESTS = ROOT / "tests"
PROGRESS = TESTS / "progress.yaml"
TEST_NAME = re.compile(r"^test_([A-Z])(\d\d)_")


def referenced_cases() -> dict[str, list[str]]:
    refs: dict[str, list[str]] = {}
    for path in sorted(TESTS.rglob("test_*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                m = TEST_NAME.match(node.name)
                if m:
                    case = f"{m.group(1)}-{m.group(2)}"
                    refs.setdefault(case, []).append(f"{path.relative_to(ROOT).as_posix()}::{node.name}")
    return refs


def main() -> int:
    catalog = yaml.safe_load((TESTS / "catalog.yaml").read_text(encoding="utf-8"))["cases"]
    done = set(yaml.safe_load(PROGRESS.read_text(encoding="utf-8"))["completed_tasks"] or [])
    refs = referenced_cases()
    problems: list[str] = []

    if build_catalog.main(["--check"]) != 0:
        problems.append("tests/catalog.yaml does not match the design and plan")
    for case in sorted(refs):
        if case not in catalog:
            problems.append(f"{case} is not a catalog case: {refs[case][0]}")
    due = sorted(c for c, entry in catalog.items() if entry["tasks"] and set(entry["tasks"]) <= done)
    for case in due:
        if case not in refs:
            problems.append(f"{case} is due (tasks {', '.join(catalog[case]['tasks'])} done) but has no test")

    covered = sum(1 for c in catalog if c in refs)
    print(f"catalog: {len(catalog)} cases, {covered} with tests, {len(due)} due, {len(done)} tasks complete")
    for problem in problems:
        print(problem)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
