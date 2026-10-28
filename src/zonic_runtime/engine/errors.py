"""Exceptions to error-catalog codes, for retry policies, error handlers and run results (design 8.8)."""

from __future__ import annotations

from langgraph.errors import GraphRecursionError, NodeTimeoutError, is_invalid_resume

from zonic_runtime.errors import ZonicError

#: Exceptions LangGraph raises that have a catalog code.
TIMEOUT_CODE = "NODE_TIMEOUT"
RECURSION_CODE = "LOOP_LIMIT_EXCEEDED"
#: A resume value the interrupt's answer model rejects (the API checks answers first; P4-02).
INVALID_ANSWER_CODE = "INTERRUPT_ANSWER_INVALID"
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
    if is_invalid_resume(error):
        return INVALID_ANSWER_CODE
    return UNKNOWN_CODE
