"""This process as the run leases name it (design 9.5, 9.6; plan P4-05), after the Node.js runtime's
``run-instance.ts``: an ID unique among the instances, and the internal address other instances
proxy a run's events from.
"""

from __future__ import annotations

import os
import socket
import uuid
from dataclasses import dataclass
from typing import Any

from zonic_runtime.config.env import internal_address
from zonic_runtime.models.resources.settings import ApiSettings


@dataclass(frozen=True)
class RunInstance:
    id: str
    address: str

    @classmethod
    def of(cls, settings: ApiSettings) -> RunInstance:
        """``api.internalAddress``, else this host's name and ``api.internalPort``."""
        host = socket.gethostname()
        return cls(
            id=f"{host}:{os.getpid()}:{uuid.uuid4()}",
            address=settings.internal_address or internal_address(host, settings.internal_port),
        )

    def lease(self, seconds: int) -> dict[str, Any]:
        """The lease of a run started, resumed or claimed here (``sp_run_start``, ``sp_run_transition``)."""
        return {"leaseOwner": self.id, "leaseAddress": self.address, "leaseSeconds": seconds}

    def wire(self) -> dict[str, str]:
        """``sp_run_claim``'s ``p_instance``."""
        return {"instanceId": self.id, "address": self.address}
