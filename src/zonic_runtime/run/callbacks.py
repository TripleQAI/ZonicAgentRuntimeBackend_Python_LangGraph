"""Signed completion callbacks (design 9.5 step 5; plan P4-05), after the Node.js runtime's
``callbacks.ts``.

When a run with ``options.callback`` ends, its summary (run, session, status, result, error code;
never its messages) is posted through the egress guard, signed in
``asyncRuns.callbackSignatureHeader`` as ``sha256=<hex HMAC-SHA256 of the body>`` with the key the
callback's secret reference names. A failed call or a non-2xx answer is retried with backoff up to
``asyncRuns.callbackRetryAttempts`` times; the run is not affected.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from collections.abc import Mapping
from typing import Any

from zonic_runtime.errors import ZonicError
from zonic_runtime.models.resources.settings import AsyncRunSettings
from zonic_runtime.ports.egress import EgressPort, EgressRequest
from zonic_runtime.ports.secrets import SecretPort, secret_name

_POST, _JSON = "POST", "application/json"
_OK = range(200, 300)


def signature(key: str, body: str) -> str:
    return "sha256=" + hmac.new(key.encode(), body.encode(), hashlib.sha256).hexdigest()


class CompletionCallbacks:
    def __init__(self, egress: EgressPort, secrets: SecretPort, settings: AsyncRunSettings) -> None:
        self._egress = egress
        self._secrets = secrets
        self._settings = settings

    async def send(self, target: Any, summary: Mapping[str, Any]) -> bool:
        """Posts the summary; ``True`` once delivered, ``False`` when every attempt failed."""
        if not isinstance(target, Mapping) or not isinstance(target.get("url"), str):
            return False
        name = secret_name(target.get("secretRef"))
        key = self._secrets.get(name) if name else None
        if key is None:
            return False
        body = json.dumps(dict(summary), ensure_ascii=False, separators=(",", ":"), default=str)
        headers = (("content-type", _JSON), (self._settings.callback_signature_header, signature(key, body)))
        request = EgressRequest(
            method=_POST,
            url=target["url"],
            headers=headers,
            body=body,
            timeout_seconds=self._settings.callback_timeout_milliseconds / 1000,
        )
        attempts = max(1, self._settings.callback_retry_attempts)
        for attempt in range(1, attempts + 1):
            try:
                if (await self._egress.send(request)).status in _OK:
                    return True
            except ZonicError:  # refused or failed: retried like a non-2xx answer
                pass
            if attempt < attempts:
                await asyncio.sleep(self._settings.callback_backoff_milliseconds * attempt / 1000)
        return False
