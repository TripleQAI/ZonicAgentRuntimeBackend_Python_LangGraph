# CLAUDE.md

Backend of the Zonic Agent Runtime on the Python LangGraph Graph API. The runtime UI is deferred.

- Design: `docs/zonic-agent-runtime-langgraph-architecture-design.md` (revision 2)
- Plan: `docs/zonic-agent-runtime-langgraph-implementation-plan.md` (revision 2). Its Section 2 amends the design.
- Build log: `docs/build-log.md` (what was built per task, deviations, checkpoint results)

## Commands (plan Section 18)

```
uv sync                                   # install (Python 3.13)
uv run ruff check . ; uv run ruff format --check .
uv run mypy
uv run pytest                             # unit, contract, principle tests (offline)
uv run lint-imports
uv run python -m scripts.gen --check      # generated/ is current
uv run python -m scripts.catalog_check    # every catalog case has a test
uv run pytest -m sql                      # opt-in: SQL scripts on a local PostgreSQL in Docker
uv run pytest -m spike                    # opt-in: Phase 0 LangGraph verification spikes
```

## Rules (plan Section 6.5, design Section 16.4)

Never: read `os.environ` outside `src/zonic_runtime/config/env.py`; add node type, handle, model, URL, limit or SQL
literals to `src/`; call `print` or `logging` outside `observability/logging_policy.py`; import a database driver; add
scheduling code outside LangGraph; write state to the database during a turn; invoke a compiled graph from a kind except
through `invoke_child`; catch `Exception` broadly in `engine/`; put secrets in documents, fixtures or tests.

A test proving a catalog case has the case ID in its name, for example `test_E03_exclusive_branches_join_once`.
