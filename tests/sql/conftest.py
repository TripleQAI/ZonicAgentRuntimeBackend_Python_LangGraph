"""SQL tests run scripts through psql (no database driver, design rule 6).

The psql command comes from, in order:

* ``ZONIC_SQL_TEST_PSQL``: a full command line, as CI sets it;
* ``ZONIC_SQL_TEST_DSN`` (environment or the git-ignored ``.env``): psql runs in
  the ``postgres:17`` Docker image and receives the DSN through its environment,
  so the DSN never appears on a command line.

Scripts run inside one transaction that ends in ROLLBACK, so a shared branch is
left unchanged. The builder's agent_builder scripts are installed first, the way a
real database has them. Checks insert ``(name, ok)`` into the temporary ``_check``
table and print as ``check|<name>|<true or false>``.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
from collections import defaultdict
from collections.abc import Callable
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
DB = ROOT / "db"
BUILDER_DB = ROOT / "tests" / "fixtures" / "builder_db"
PSQL_IMAGE = "postgres:17"

Psql = Callable[[str], str]
Rows = dict[str, list[tuple[str, ...]]]


def _dotenv(name: str) -> str | None:
    path = ROOT / ".env"
    if not path.is_file():
        return None
    for line in path.read_text(encoding="utf-8").splitlines():
        key, sep, value = line.partition("=")
        if sep and key.strip() == name:
            return value.strip()
    return None


def _command() -> tuple[list[str], dict[str, str]]:
    env = dict(os.environ)
    explicit = env.get("ZONIC_SQL_TEST_PSQL")
    if explicit:
        return shlex.split(explicit), env
    dsn = env.get("ZONIC_SQL_TEST_DSN") or _dotenv("ZONIC_SQL_TEST_DSN")
    if not dsn:
        pytest.skip("set ZONIC_SQL_TEST_PSQL or ZONIC_SQL_TEST_DSN to run SQL tests")
    env["ZONIC_SQL_TEST_DSN"] = dsn
    docker = ["docker", "run", "--rm", "-i", "-e", "ZONIC_SQL_TEST_DSN", PSQL_IMAGE]
    return [*docker, "sh", "-c", 'exec psql "$ZONIC_SQL_TEST_DSN" "$@"', "psql"], env


@pytest.fixture(scope="session")
def psql() -> Psql:
    base, env = _command()

    def run(script: str) -> str:
        done = subprocess.run(
            [*base, "-X", "-q", "-At", "-v", "ON_ERROR_STOP=1"],
            input=script,
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=env,
            timeout=300,
            check=False,
        )
        if done.returncode != 0:
            pytest.fail(f"psql exited {done.returncode}:\n{done.stderr}")
        return done.stdout

    return run


RUNTIME_FILES = (
    "001_schema.sql",
    "002_functions_triggers.sql",
    "003_indexes.sql",
    "004_views.sql",
    "005_procedures.sql",
)

CHECK_SETUP = r"""
CREATE TEMP TABLE _check (name text PRIMARY KEY, ok boolean NOT NULL) ON COMMIT DROP;

-- Runs one statement and records whether it failed with the expected SQLSTATE and, unless
-- p_reason is NULL, the expected message (the reason token for the procedures).
CREATE FUNCTION pg_temp.expect_error(p_name text, p_sql text, p_sqlstate text, p_reason text)
RETURNS void LANGUAGE plpgsql AS $$
BEGIN
    BEGIN
        EXECUTE p_sql;
    EXCEPTION WHEN OTHERS THEN
        INSERT INTO _check VALUES (p_name, SQLSTATE = p_sqlstate AND (p_reason IS NULL OR SQLERRM = p_reason));
        RETURN;
    END;
    INSERT INTO _check VALUES (p_name, false);
END;
$$;
"""


def builder_schema() -> str:
    """The builder's own agent_builder scripts, vendored by ``scripts/sync_sql.py``."""
    source = json.loads((BUILDER_DB / "SOURCE.json").read_text(encoding="utf-8"))
    return "\n".join((BUILDER_DB / name).read_text(encoding="utf-8") for name in source["files"])


def runtime_schema() -> str:
    """agent_runtime 001 to 005, in run_all.sql order."""
    return "\n".join((DB / "runtime" / name).read_text(encoding="utf-8") for name in RUNTIME_FILES)


def observability_schema() -> str:
    """agent_observability 001 to 005, in run_all.sql order."""
    files = sorted((DB / "observability").glob("00[1-5]_*.sql"))
    return "\n".join(path.read_text(encoding="utf-8") for path in files)


def run_checks(psql: Psql, checks: str, install: str) -> Rows:
    """Run ``install``, then ``checks``, print ``_check`` and roll everything back."""
    report = "SELECT 'check|' || name || '|' || ok FROM _check ORDER BY name;"
    parts = ["BEGIN;", install, CHECK_SETUP, checks, report]
    rows: Rows = defaultdict(list)
    for line in psql("\n".join([*parts, "ROLLBACK;"])).splitlines():
        kind, _, rest = line.partition("|")
        if rest:
            rows[kind].append(tuple(rest.split("|")))
    return rows


def passed(rows: Rows) -> dict[str, bool]:
    return {name: ok == "true" for name, ok in rows["check"]}
