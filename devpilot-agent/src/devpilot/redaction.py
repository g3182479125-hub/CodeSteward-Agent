"""Shared redaction for data leaving the execution boundary."""

from __future__ import annotations

import re
from typing import cast

_SENSITIVE_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9]{12,}\b"),
    re.compile(r"\be2b_[A-Za-z0-9]{16,}\b"),
    re.compile(r"(?i)authorization\s*:\s*bearer\s+\S+"),
    re.compile(r"(?i)\b(?:deepseek|e2b|api)[_-]?(?:api)?[_-]?key\s*[=:]\s*\S+"),
)


def redact_sensitive_text(value: str) -> str:
    """Replace high-confidence credentials while retaining bounded diagnostics."""

    result = value
    for pattern in _SENSITIVE_PATTERNS:
        result = pattern.sub("[REDACTED]", result)
    return result


def redact_sensitive_payload(value: object) -> object:
    """Recursively redact strings and sensitive mapping values before persistence."""
    if isinstance(value, str):
        return redact_sensitive_text(value)
    if isinstance(value, dict):
        result: dict[str, object] = {}
        for key, item in cast(dict[str, object], value).items():
            if any(
                marker in key.casefold()
                for marker in ("token", "secret", "password", "api_key")
            ):
                result[key] = "[REDACTED]"
            else:
                result[key] = redact_sensitive_payload(item)
        return result
    if isinstance(value, (list, tuple)):
        items = cast(list[object] | tuple[object, ...], value)
        return [redact_sensitive_payload(item) for item in items]
    return value
