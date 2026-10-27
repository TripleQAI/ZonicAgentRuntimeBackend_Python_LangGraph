"""Exceptions to error-catalog codes, for retry policies, error handlers and run results (design 8.8)."""

from __future__ import annotations

from langgraph.errors import GraphRecursionError, NodeTimeoutError

from zonic_runtime.errors import ZonicError

#: Exceptions LangGraph raises that have a catalog code.
TIMEOUT_CODE = "NODE_TIMEOUT"
RECURSION_CODE = "LOOP_LIMIT_EXCEEDED"
UNKNOWN_CODE = "INTERNAL"
#: Codes that end the run whatever the node's error policy: a policy must not keep a run
#: going past its limits, its deadline or a cancellation.
RUN_FATAL = frozenset({"LOOP_LIMIT_EXCEEDED", "RUN_TIMEOUT", "CANCELLED"})


def error_code(error: BaseException) -> str:
    if isinstance(error, ZonicError):
        return error.code
    if isinstance(error, NodeTimeoutError):
        return TIMEOUT_CODE
    if isinstance(error, GraphRecursionError):
        return RECURSION_CODE
    return UNKNOWN_CODE
