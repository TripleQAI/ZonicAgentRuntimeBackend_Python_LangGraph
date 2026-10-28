"""The logging policy (design 12, "Library logging"; plan P2-07, case L-13).

The runtime writes no logs of its own. Libraries log through ``logging``; one handler, installed
at start-up, drops records below ``WARNING`` and attaches warnings and errors that happen inside a
run to that run's trace as ``event`` observations. Outside a run they are dropped. This is the only
module that imports ``logging`` (rule 8).
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from contextvars import ContextVar
from typing import Any

#: ``(name, level, metadata)``: the current run's event sink, set by the run's trace.
RunEventSink = Callable[[str, str, Mapping[str, Any]], None]
CURRENT_RUN_EVENTS: ContextVar[RunEventSink | None] = ContextVar("zonic_run_events", default=None)
_MAX_MESSAGE = 2000


class RunEventHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        sink = CURRENT_RUN_EVENTS.get()
        if sink is None:
            return
        level = "ERROR" if record.levelno >= logging.ERROR else "WARNING"
        try:
            sink(
                f"log.{record.name}",
                level,
                {"logger": record.name, "message": record.getMessage()[:_MAX_MESSAGE]},
            )
        except Exception:  # noqa: BLE001  # a broken sink must never break the library that logged
            return


def install() -> Callable[[], None]:
    """Routes every logger through ``RunEventHandler``; returns a function that restores them."""
    root = logging.getLogger()
    previous = (list(root.handlers), root.level)
    for handler in previous[0]:
        root.removeHandler(handler)
    handler = RunEventHandler(level=logging.WARNING)
    root.addHandler(handler)
    root.setLevel(logging.WARNING)
    logging.captureWarnings(True)

    def restore() -> None:
        root.removeHandler(handler)
        for item in previous[0]:
            root.addHandler(item)
        root.setLevel(previous[1])
        logging.captureWarnings(False)

    return restore
