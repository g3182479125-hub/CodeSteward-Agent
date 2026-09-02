"""Server-owned P6 execution attestation used by the control plane."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import cast


@dataclass(frozen=True, slots=True)
class P6ExecutionGate:
    backend: str
    capability_hash: str
    suite_version: str
    expires_at: datetime

    @classmethod
    def load(cls, record_path: str | Path) -> P6ExecutionGate | None:
        path = Path(record_path).expanduser().resolve()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return None
        if not isinstance(raw, dict):
            return None
        values = cast(dict[str, object], raw)
        backend = values.get("backend")
        capability_hash = values.get("capability_hash")
        suite_version = values.get("suite_version")
        expires_at = values.get("expires_at")
        if not (
            isinstance(backend, str)
            and backend
            and isinstance(capability_hash, str)
            and capability_hash
            and isinstance(suite_version, str)
            and suite_version
            and isinstance(expires_at, str)
            and expires_at
        ):
            return None
        try:
            expiry = datetime.fromisoformat(expires_at.replace("Z", "+00:00")).astimezone(UTC)
        except ValueError:
            return None
        if len(capability_hash) != 64 or any(
            char not in "0123456789abcdef" for char in capability_hash.lower()
        ):
            return None
        return cls(backend, capability_hash, suite_version, expiry)

    def permits(self, backend: str) -> bool:
        return backend == self.backend and datetime.now(UTC) < self.expires_at
