"""Stable errors raised by the DevPilot domain layer."""

from __future__ import annotations


class DomainStateError(ValueError):
    """Raised when a domain object receives an illegal state transition."""

    code = "INVALID_STATE_TRANSITION"

    def __init__(self, entity: str, current: str, target: str) -> None:
        self.entity = entity
        self.current = current
        self.target = target
        super().__init__(f"{entity} cannot transition from {current} to {target}")
