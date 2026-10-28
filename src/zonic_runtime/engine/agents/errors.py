"""Model provider failures as catalog errors (design 8.8; plan P2-02, case N-08).

A provider SDK's own errors (throttling, 5xx, connection loss) become ``MODEL_UNAVAILABLE``,
which the LLM and agent nodes' ``RetryPolicy`` retries; when the retries are used up the run
fails with it. An answer that is not the structured output asked for (LangChain's
``StructuredOutputValidationError``, native structured output) is ``STRUCTURED_OUTPUT_INVALID``.
Only the providers' error classes are caught (rule 17): a bug in the runtime
still propagates as itself. The classes come from the provider packages that are installed.
"""

from __future__ import annotations

import importlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from langchain.agents.structured_output import StructuredOutputValidationError

from zonic_runtime.errors import ZonicError

UNAVAILABLE, STRUCTURED = "MODEL_UNAVAILABLE", "STRUCTURED_OUTPUT_INVALID"
#: ``module:class`` of every provider error base class.
_PROVIDER_ERRORS = (
    "botocore.exceptions:ClientError",
    "botocore.exceptions:BotoCoreError",
    "openai:APIError",
    "anthropic:APIError",
    "httpx:HTTPError",
)


def _load() -> tuple[type[BaseException], ...]:
    found: list[type[BaseException]] = []
    for name in _PROVIDER_ERRORS:
        module, _, cls = name.partition(":")
        try:
            found.append(getattr(importlib.import_module(module), cls))
        except (ImportError, AttributeError):
            continue
    return tuple(found)


PROVIDER_ERRORS = _load()


@asynccontextmanager
async def provider_errors(node_id: str) -> AsyncIterator[None]:
    try:
        yield
    except PROVIDER_ERRORS as error:
        raise ZonicError(
            UNAVAILABLE, node_id=node_id, reason=f"{type(error).__name__}: {error}"[:300]
        ) from error
    except StructuredOutputValidationError as error:
        raise ZonicError(STRUCTURED, node_id=node_id, reason=str(error)[:300]) from error
