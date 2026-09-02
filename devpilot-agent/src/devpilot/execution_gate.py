"""Server-owned P6 execution attestation used by the control plane."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

_SERVER_ATTESTATION = object()


@dataclass(frozen=True, slots=True, init=False)
class P6ExecutionGate:
    backend: str
    capability_hash: str
    suite_version: str
    expires_at: datetime

    def __init__(
        self,
        backend: str,
        capability_hash: str,
        suite_version: str,
        expires_at: datetime,
        *,
        _attestation: object,
    ) -> None:
        if _attestation is not _SERVER_ATTESTATION:
            raise TypeError("P6ExecutionGate is server-attested only")
        object.__setattr__(self, "backend", backend)
        object.__setattr__(self, "capability_hash", capability_hash)
        object.__setattr__(self, "suite_version", suite_version)
        object.__setattr__(self, "expires_at", expires_at)

    def permits(self, backend: str) -> bool:
        return backend == self.backend and datetime.now(UTC) < self.expires_at
