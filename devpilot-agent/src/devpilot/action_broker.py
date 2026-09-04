"""Trusted in-process action broker for nanobot user confirmations."""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import threading
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from .redaction import redact_execution_output, redact_sensitive_text

MAX_CONFIRMATION_TTL = timedelta(minutes=5)
_ACTIONS = frozenset({"apply", "verify", "rollback"})
_CAPABILITIES = {
    "apply": "r1.apply",
    "verify": "r2.verify",
    "rollback": "r1.rollback",
}
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_DIGEST = re.compile(r"^[a-f0-9]{64}$")


class BrokerError(ValueError):
    """Stable, non-sensitive broker rejection."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _text(value: object, *, max_length: int = 128) -> str:
    if not isinstance(value, str) or not value or len(value) > max_length:
        raise BrokerError("APPROVAL_BROKER_UNAVAILABLE")
    if "\r" in value or "\n" in value:
        raise BrokerError("APPROVAL_BROKER_UNAVAILABLE")
    return value


def _identifier(value: object) -> str:
    result = _text(value)
    if _IDENTIFIER.fullmatch(result) is None:
        raise BrokerError("APPROVAL_SCOPE_MISMATCH")
    return result


def _digest(value: object) -> str:
    result = _text(value, max_length=64)
    if _DIGEST.fullmatch(result) is None:
        raise BrokerError("APPROVAL_SCOPE_MISMATCH")
    return result


@dataclass(frozen=True)
class ConfirmationContext:
    """Out-of-band, Gateway-signed evidence that a user confirmed an action."""

    session_id: str
    thread_id: str
    user_message_seq: int
    confirmation_id: str
    issued_at: str
    expires_at: str
    user_event_nonce: str
    signature: str

    @classmethod
    def from_mapping(cls, value: object) -> ConfirmationContext:
        if not isinstance(value, Mapping):
            raise BrokerError("APPROVAL_BROKER_UNAVAILABLE")
        mapping = cast(Mapping[str, object], value)
        expected = {
            "session_id",
            "thread_id",
            "user_message_seq",
            "confirmation_id",
            "issued_at",
            "expires_at",
            "user_event_nonce",
            "signature",
        }
        if set(mapping) != expected:
            raise BrokerError("APPROVAL_BROKER_UNAVAILABLE")
        sequence = mapping["user_message_seq"]
        if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 1:
            raise BrokerError("APPROVAL_BROKER_UNAVAILABLE")
        signature = _text(mapping["signature"], max_length=128)
        if re.fullmatch(r"[0-9a-f]{64}", signature) is None:
            raise BrokerError("APPROVAL_BROKER_UNAVAILABLE")
        return cls(
            session_id=_identifier(mapping["session_id"]),
            thread_id=_identifier(mapping["thread_id"]),
            user_message_seq=sequence,
            confirmation_id=_identifier(mapping["confirmation_id"]),
            issued_at=_text(mapping["issued_at"], max_length=64),
            expires_at=_text(mapping["expires_at"], max_length=64),
            user_event_nonce=_identifier(mapping["user_event_nonce"]),
            signature=signature,
        )

    def validate_time(self, now: datetime) -> tuple[datetime, datetime]:
        try:
            issued = datetime.fromisoformat(self.issued_at.replace("Z", "+00:00"))
            expires = datetime.fromisoformat(self.expires_at.replace("Z", "+00:00"))
        except ValueError as exc:
            raise BrokerError("APPROVAL_BROKER_UNAVAILABLE") from exc
        if issued.tzinfo is None or expires.tzinfo is None:
            raise BrokerError("APPROVAL_BROKER_UNAVAILABLE")
        issued = issued.astimezone(UTC)
        expires = expires.astimezone(UTC)
        if expires <= issued or expires - issued > MAX_CONFIRMATION_TTL:
            raise BrokerError("APPROVAL_BROKER_UNAVAILABLE")
        if now < issued or now >= expires:
            raise BrokerError("APPROVAL_CONFIRMATION_EXPIRED")
        return issued, expires


@dataclass(frozen=True)
class BrokerActionRequest:
    """Model-visible action fields plus the hidden signed ConfirmationContext."""

    context: ConfirmationContext
    action: str
    change_set_id: str
    plan_hash: str
    patch_hash: str
    scope_hash: str
    project_id: str
    verification_profile: str | None = None

    def validate(self) -> None:
        if self.action not in _ACTIONS:
            raise BrokerError("CAPABILITY_INVALID")
        _identifier(self.change_set_id)
        _digest(self.plan_hash)
        _digest(self.patch_hash)
        _digest(self.scope_hash)
        _identifier(self.project_id)
        if self.verification_profile is not None:
            _identifier(self.verification_profile)


@dataclass(frozen=True)
class BrokerActionResult:
    status: str
    change_set_id: str
    action: str
    run_id: str | None = None
    error_code: str | None = None
    result_ref: str | None = None

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "status": self.status,
            "change_set_id": self.change_set_id,
            "action": self.action,
        }
        if self.run_id is not None:
            result["run_id"] = self.run_id
        if self.error_code is not None:
            result["error_code"] = self.error_code
        if self.result_ref is not None:
            result["result_ref"] = self.result_ref
        return result


def canonical_confirmation_string(
    context: ConfirmationContext,
    *,
    change_set_id: str,
    plan_hash: str,
    patch_hash: str,
    scope_hash: str,
    project_id: str,
    capability: str,
) -> str:
    """Build the exact newline-delimited HMAC input frozen by ADR-0003."""
    if capability not in _CAPABILITIES.values():
        raise BrokerError("CAPABILITY_INVALID")
    return "\n".join(
        (
            "v1",
            context.session_id,
            context.thread_id,
            str(context.user_message_seq),
            context.confirmation_id,
            _identifier(change_set_id),
            _digest(plan_hash),
            _digest(patch_hash),
            _digest(scope_hash),
            _identifier(project_id),
            capability,
            context.issued_at,
            context.expires_at,
            context.user_event_nonce,
        )
    )


def sign_confirmation_context(
    context: ConfirmationContext,
    gateway_key: bytes,
    *,
    change_set_id: str,
    plan_hash: str,
    patch_hash: str,
    scope_hash: str,
    project_id: str,
    capability: str,
) -> str:
    if len(gateway_key) != 32:
        raise ValueError("gateway key must be 32 bytes")
    payload = canonical_confirmation_string(
        context,
        change_set_id=change_set_id,
        plan_hash=plan_hash,
        patch_hash=patch_hash,
        scope_hash=scope_hash,
        project_id=project_id,
        capability=capability,
    )
    return hmac.new(gateway_key, payload.encode("utf-8"), hashlib.sha256).hexdigest()


SnapshotReader = Callable[[str], Mapping[str, Any]]
CapabilityIssuer = Callable[[str, str], str]
CapabilityExecutor = Callable[[str, str, str, str, str | None], Mapping[str, Any]]
Clock = Callable[[], datetime]


class ActionBroker:
    """Execute one signed user action inside the trusted nanobot process."""

    def __init__(
        self,
        gateway_key: bytes,
        *,
        snapshot_reader: SnapshotReader,
        capability_issuer: CapabilityIssuer,
        capability_executor: CapabilityExecutor,
        clock: Clock | None = None,
    ) -> None:
        if len(gateway_key) != 32:
            raise ValueError("gateway key must be 32 bytes")
        self._gateway_key = bytes(gateway_key)
        self._snapshot_reader = snapshot_reader
        self._capability_issuer = capability_issuer
        self._capability_executor = capability_executor
        self._clock = clock or (lambda: datetime.now(UTC))
        self._lock = threading.Lock()
        self._consumed: set[tuple[str, str, str, str]] = set()
        self._last_sequence: dict[tuple[str, str], int] = {}

    @classmethod
    def new_gateway_key(cls) -> bytes:
        return secrets.token_bytes(32)

    def execute(self, request: BrokerActionRequest) -> BrokerActionResult:
        try:
            request.validate()
            now = self._clock().astimezone(UTC)
            request.context.validate_time(now)
            capability = _CAPABILITIES[request.action]
            expected = sign_confirmation_context(
                request.context,
                self._gateway_key,
                change_set_id=request.change_set_id,
                plan_hash=request.plan_hash,
                patch_hash=request.patch_hash,
                scope_hash=request.scope_hash,
                project_id=request.project_id,
                capability=capability,
            )
            if not hmac.compare_digest(expected, request.context.signature):
                raise BrokerError("APPROVAL_BROKER_UNAVAILABLE")
            self._consume_confirmation(request)
            self._check_snapshot(request)
            token = self._capability_issuer(request.action, request.change_set_id)
            if not token or len(token) > 512:
                raise BrokerError("APPROVAL_BROKER_UNAVAILABLE")
            idempotency_key = f"broker-{uuid.uuid4().hex}"
            try:
                raw = self._capability_executor(
                    request.action,
                    request.change_set_id,
                    token,
                    idempotency_key,
                    request.verification_profile,
                )
            finally:
                token = ""
            return self._result_from_executor(request, raw)
        except BrokerError as exc:
            return BrokerActionResult(
                status="rejected",
                change_set_id=request.change_set_id,
                action=request.action,
                error_code=exc.code,
            )
        except (KeyError, TypeError, ValueError, OSError, RuntimeError):
            return BrokerActionResult(
                status="rejected",
                change_set_id=request.change_set_id,
                action=request.action,
                error_code="APPROVAL_BROKER_UNAVAILABLE",
            )

    def _consume_confirmation(self, request: BrokerActionRequest) -> None:
        context = request.context
        replay_key = (
            context.session_id,
            context.thread_id,
            context.confirmation_id,
            context.user_event_nonce,
        )
        sequence_key = (context.session_id, context.thread_id)
        with self._lock:
            if replay_key in self._consumed:
                raise BrokerError("APPROVAL_CONFIRMATION_REPLAYED")
            if context.user_message_seq <= self._last_sequence.get(sequence_key, 0):
                raise BrokerError("APPROVAL_CONFIRMATION_REPLAYED")
            self._consumed.add(replay_key)
            self._last_sequence[sequence_key] = context.user_message_seq

    def _check_snapshot(self, request: BrokerActionRequest) -> None:
        snapshot = self._snapshot_reader(request.change_set_id)
        expected = {
            "change_set_id": request.change_set_id,
            "plan_hash": request.plan_hash,
            "patch_hash": request.patch_hash,
            "scope_hash": request.scope_hash,
            "project_id": request.project_id,
        }
        if any(snapshot.get(key) != value for key, value in expected.items()):
            raise BrokerError("APPROVAL_SCOPE_MISMATCH")

    @staticmethod
    def _result_from_executor(
        request: BrokerActionRequest, raw: Mapping[str, Any]
    ) -> BrokerActionResult:
        status = raw.get("status")
        status = ActionBroker._safe_result_identifier(status)
        if status is None:
            raise BrokerError("APPROVAL_BROKER_UNAVAILABLE")
        values: dict[str, str | None] = {}
        for key in ("run_id", "error_code", "result_ref"):
            value = raw.get(key)
            if value is None:
                values[key] = None
            else:
                safe_value = ActionBroker._safe_result_identifier(value)
                if safe_value is None:
                    raise BrokerError("APPROVAL_BROKER_UNAVAILABLE")
                values[key] = safe_value
        if status == "completed" and values["run_id"] is None and values["result_ref"] is None:
            raise BrokerError("APPROVAL_BROKER_UNAVAILABLE")
        return BrokerActionResult(
            status=status,
            change_set_id=request.change_set_id,
            action=request.action,
            run_id=values["run_id"],
            error_code=values["error_code"],
            result_ref=values["result_ref"],
        )

    @staticmethod
    def _safe_result_identifier(value: object) -> str | None:
        if not isinstance(value, str) or len(value) > 128:
            return None
        value = redact_sensitive_text(value)
        value = redact_execution_output(value)
        if "[REDACTED" in value or _IDENTIFIER.fullmatch(value) is None:
            return None
        return value
