"""The one output the runtime writes outside Langfuse: a start-up failure (design Section 12, rule 8)."""

from __future__ import annotations

import json
import sys
from typing import NoReturn

from zonic_runtime.errors import StartupError

EXIT_STARTUP_FAILURE = 78  # EX_CONFIG


def fail_startup(error: StartupError) -> NoReturn:
    sys.stderr.write(json.dumps({"startupError": error.code, "details": error.details}, default=str) + "\n")
    sys.stderr.flush()
    raise SystemExit(EXIT_STARTUP_FAILURE)
