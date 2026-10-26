"""Phase 0 verification spikes (P0-10, design Section 20.1). Deleted after M0.

Run with ``uv run pytest spikes -m spike -s``; ``-s`` shows the measurements each
spike prints. Each spike's findings are written up in ``spikes/reports/`` and the
decisions in ``docs/decisions/0001-langgraph-verification-spikes.md``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

SPIKES = Path(__file__).resolve().parent


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        if SPIKES in Path(str(item.fspath)).parents:
            item.add_marker(pytest.mark.spike)
