"""Design Section 16.4 rules 1 to 18 as AST checks over ``src/zonic_runtime`` (P0-06): case X-08.

A line may opt out of one rule with ``# principle: allow <rule> <reason>``; the
reason is required so every exception is visible in review.
"""

from __future__ import annotations

import ast
import json
import re
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src" / "zonic_runtime"
MANIFEST = json.loads((ROOT / "resources" / "node-runtime.json").read_text(encoding="utf-8"))
NODE_TYPES = frozenset(MANIFEST["nodeTypes"])
HANDLES = frozenset(
    {"default", "pass", "fail", "approved", "rejected", "error", "r_else", "r-else", "r_default"}
)
SQL_RE = re.compile(
    r"^\s*(SELECT|INSERT|UPDATE|DELETE|CALL|CREATE|DROP|ALTER|TRUNCATE|GRANT)\b", re.IGNORECASE
)
URL_RE = re.compile(r"^(https?|wss?)://", re.IGNORECASE)
MODEL_RE = re.compile(r"^(anthropic\.|us\.anthropic\.|claude-|gpt-[0-9]|o[0-9]-|gemini-)", re.IGNORECASE)
GRAPH_WORDS = ("graph", "agent", "swarm", "subflow", "child")
ALLOW_RE = re.compile(r"#\s*principle:\s*allow\s+(\S+)\s+\S+")

#: Modules where routing and lifecycle code lives (rule 2).
ROUTING_SCOPE = (
    "workflow/compiler/",
    "engine/routing.py",
    "engine/node_template.py",
    "engine/error_policy.py",
)
#: Modules that run during a turn and must not block the event loop (rule 13).
NON_BLOCKING_SCOPE = (
    "engine/",
    "run/",
    "api/",
    "application/",
    "hooks/",
    "integrations/",
    "workflow/",
    "io/",
)
#: Modules that must not keep module-level mutable state (rule 14).
NO_MUTABLE_STATE_SCOPE = ("engine/", "workflow/", "run/", "hooks/", "application/")


@dataclass(frozen=True)
class Violation:
    rule: str
    file: str
    line: int
    text: str

    def __str__(self) -> str:
        return f"{self.file}:{self.line} [{self.rule}] {self.text}"


def _sources() -> Iterator[tuple[str, str, ast.Module]]:
    for path in sorted(SRC.rglob("*.py")):
        rel = path.relative_to(SRC).as_posix()
        text = path.read_text(encoding="utf-8")
        yield rel, text, ast.parse(text, filename=rel)


def _allowed(lines: list[str], lineno: int, rule: str) -> bool:
    if 0 < lineno <= len(lines):
        m = ALLOW_RE.search(lines[lineno - 1])
        return bool(m and m.group(1) == rule)
    return False


def _docstring_nodes(tree: ast.Module) -> set[int]:
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                ids.add(id(body[0].value))
    return ids


def _string_constants(tree: ast.Module) -> Iterator[ast.Constant]:
    docs = _docstring_nodes(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docs:
            yield node


def _in_scope(rel: str, scope: tuple[str, ...]) -> bool:
    return any(rel == s or rel.startswith(s) for s in scope)


def collect() -> list[Violation]:
    found: list[Violation] = []

    def add(rule: str, rel: str, lines: list[str], node: ast.AST, text: str) -> None:
        lineno = getattr(node, "lineno", 0)
        if not _allowed(lines, lineno, rule):
            found.append(Violation(rule, rel, lineno, text))

    for rel, text, tree in _sources():
        lines = text.splitlines()
        for const in _string_constants(tree):
            value = const.value
            if value in NODE_TYPES:
                add("rule-1-node-type", rel, lines, const, f"node type literal {value!r}")
            if _in_scope(rel, ROUTING_SCOPE) and value in HANDLES:
                add("rule-2-handle", rel, lines, const, f"handle literal {value!r}")
            if URL_RE.match(value) and rel != "config/env.py":
                add("rule-3-url", rel, lines, const, f"URL literal {value!r}")
            if MODEL_RE.match(value):
                add("rule-3-model", rel, lines, const, f"model ID literal {value!r}")
            if SQL_RE.match(value):
                add("rule-5-sql", rel, lines, const, f"SQL text {value[:40]!r}")

        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                names = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ""]
                for name in names:
                    root = name.split(".")[0]
                    if root in {"psycopg", "psycopg2", "asyncpg", "pyodbc", "sqlalchemy"} or name.startswith(
                        "langgraph.checkpoint.postgres"
                    ):
                        add("rule-5-driver", rel, lines, node, f"database driver import {name!r}")
                    if (
                        root in {"logging", "loguru", "structlog"}
                        and rel != "observability/logging_policy.py"
                    ):
                        add("rule-8-logging", rel, lines, node, f"logging import {name!r}")
                    if root == "requests" or name == "urllib.request":
                        add("rule-13-blocking", rel, lines, node, f"blocking HTTP client {name!r}")
                    if rel.startswith("engine/kinds/") and root in {"langfuse", "opentelemetry"}:
                        add("rule-9-tracing", rel, lines, node, f"tracing import in a node kind {name!r}")

            if (
                isinstance(node, ast.Attribute)
                and node.attr in {"environ", "getenv"}
                and isinstance(node.value, ast.Name)
                and node.value.id == "os"
                and rel != "config/env.py"
            ):
                add("rule-4-environ", rel, lines, node, "os.environ/os.getenv outside config/env.py")

            if isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Name) and func.id == "print":
                    add("rule-8-print", rel, lines, node, "print() call")
                if (
                    isinstance(func, ast.Attribute)
                    and func.attr == "sleep"
                    and isinstance(func.value, ast.Name)
                    and func.value.id == "time"
                ):
                    add("rule-13-blocking", rel, lines, node, "time.sleep() call")
                if _in_scope(rel, NON_BLOCKING_SCOPE) and (
                    (isinstance(func, ast.Name) and func.id == "open")
                    or (
                        isinstance(func, ast.Attribute)
                        and func.attr in {"read_text", "read_bytes", "write_text"}
                    )
                ):
                    add("rule-13-blocking", rel, lines, node, "synchronous file I/O in a turn-path module")
                if (
                    rel.startswith("engine/kinds/")
                    and isinstance(func, ast.Attribute)
                    and func.attr in {"ainvoke", "astream", "astream_events", "invoke", "stream"}
                    and any(w in ast.unparse(func.value).lower() for w in GRAPH_WORDS)
                ):
                    add("rule-18-invoke-child", rel, lines, node, f"direct {ast.unparse(func)} on a graph")

            if isinstance(node, ast.ExceptHandler) and rel.startswith("engine/"):
                broad = node.type is None or (
                    isinstance(node.type, ast.Name) and node.type.id in {"Exception", "BaseException"}
                )
                if broad:
                    add("rule-17-broad-except", rel, lines, node, "broad except in engine/")

        if _in_scope(rel, NO_MUTABLE_STATE_SCOPE):
            for stmt in tree.body:
                targets: list[ast.expr] = []
                value: ast.expr | None = None
                if isinstance(stmt, ast.Assign):
                    targets, value = stmt.targets, stmt.value
                elif isinstance(stmt, ast.AnnAssign) and stmt.value is not None:
                    targets, value = [stmt.target], stmt.value
                if value is None:
                    continue
                mutable = isinstance(
                    value, (ast.Dict, ast.List, ast.Set, ast.DictComp, ast.ListComp, ast.SetComp)
                )
                if isinstance(value, ast.Call) and isinstance(value.func, ast.Name):
                    mutable = mutable or value.func.id in {
                        "dict",
                        "list",
                        "set",
                        "defaultdict",
                        "OrderedDict",
                    }
                names = [t.id for t in targets if isinstance(t, ast.Name)]
                if mutable and names and not all(n == "__all__" for n in names):
                    add("rule-14-module-state", rel, lines, stmt, f"module-level mutable {names}")
    return found


RULES = sorted(
    {
        "rule-1-node-type",
        "rule-2-handle",
        "rule-3-url",
        "rule-3-model",
        "rule-4-environ",
        "rule-5-sql",
        "rule-5-driver",
        "rule-8-logging",
        "rule-8-print",
        "rule-9-tracing",
        "rule-13-blocking",
        "rule-14-module-state",
        "rule-17-broad-except",
        "rule-18-invoke-child",
    }
)


@pytest.fixture(scope="module")
def violations() -> list[Violation]:
    return collect()


@pytest.mark.parametrize("rule", RULES)
def test_X08_principle_rule_holds(rule: str, violations: list[Violation]) -> None:
    hits = [str(v) for v in violations if v.rule == rule]
    assert hits == [], "\n".join(hits)


def test_X08_checker_catches_each_rule(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The checker itself: a planted file triggers every rule it should."""
    planted = tmp_path / "zonic_runtime"
    (planted / "engine" / "kinds").mkdir(parents=True)
    (planted / "engine" / "kinds" / "bad.py").write_text(
        "import os\nimport logging\nimport requests\nimport psycopg\nimport langfuse\nimport time\n"
        "CACHE = {}\n"
        "def f(graph):\n"
        "    print('x')\n"
        "    time.sleep(1)\n"
        "    os.environ.get('A')\n"
        "    q = 'SELECT 1'\n"
        "    t = 'rule'\n"
        "    u = 'https://example.com'\n"
        "    m = 'claude-sonnet-4-6'\n"
        "    graph.ainvoke({})\n"
        "    try:\n        pass\n    except Exception:\n        pass\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(sys.modules[__name__], "SRC", planted)
    rules = {v.rule for v in collect()}
    expected = set(RULES) - {"rule-2-handle"}
    assert expected <= rules, sorted(expected - rules)


def test_X08_waiver_requires_a_reason(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    planted = tmp_path / "zonic_runtime"
    planted.mkdir()
    (planted / "m.py").write_text(
        "a = 'rule'  # principle: allow rule-1-node-type documented-exception\nb = 'rule'  # principle: allow rule-1-node-type\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(sys.modules[__name__], "SRC", planted)
    lines = [v.line for v in collect() if v.rule == "rule-1-node-type"]
    assert lines == [2]


def test_Q16_a_node_calling_time_sleep_fails_the_blocking_call_rule(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    planted = tmp_path / "zonic_runtime"
    (planted / "engine" / "kinds").mkdir(parents=True)
    (planted / "engine" / "kinds" / "sleepy.py").write_text(
        "import time\n\n\nasync def execute(ctx, node_input):\n    time.sleep(0.5)\n    return None\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(sys.modules[__name__], "SRC", planted)
    assert [(v.rule, v.line) for v in collect()] == [("rule-13-blocking", 5)]
