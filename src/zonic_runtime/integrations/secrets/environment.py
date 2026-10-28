"""``EnvironmentSecrets``: secrets from the process environment (local and test runs; plan P3-02).

``secret:<name>`` reads ``ZONIC_SECRET_<NAME>`` (upper case, other characters as ``_``). AWS
Secrets Manager replaces it in AWS (P5-04).
"""

from __future__ import annotations

from zonic_runtime.config.env import environment_secret


class EnvironmentSecrets:
    def get(self, name: str) -> str | None:
        return environment_secret(name)
