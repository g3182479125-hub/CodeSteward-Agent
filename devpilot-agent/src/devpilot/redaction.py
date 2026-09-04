"""Shared redaction for data leaving the execution boundary."""

from __future__ import annotations

import re
from typing import cast

_SENSITIVE_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b"),
    re.compile(r"\be2b_[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"(?i)authorization\s*:\s*bearer\s+\S+"),
)
_SENSITIVE_NAME = (
    r"(?:[A-Za-z0-9]*(?:api[_-]?(?:key|token)|access[_-]?(?:key|token)|"
    r"auth[_-]?token|secret(?:[_-]?key)?|private[_-]?key|credential|"
    r"token|password|passwd)[A-Za-z0-9_-]*)"
)
_SENSITIVE_FIELD_MARKERS = (
    "token",
    "secret",
    "password",
    "passwd",
    "api_key",
    "apikey",
    "access_key",
    "accesskey",
    "authorization",
    "private_key",
    "privatekey",
    "credential",
)
_SENSITIVE_MULTILINE_ASSIGNMENT_PATTERN = re.compile(
    r"(?is)(?<![A-Za-z0-9])"
    rf"(?P<prefix>[\"']?{_SENSITIVE_NAME}[\"']?\s*[=:]\s*)"
    r"(?P<quote>\"\"\"|''')(?P<value>.*?)(?P=quote)"
 )
_SENSITIVE_ASSIGNMENT_PATTERN = re.compile(
    r"(?i)(?<![A-Za-z0-9])"
    rf"(?P<prefix>[\"']?{_SENSITIVE_NAME}[\"']?\s*[=:]\s*)"
    r"(?P<value>[^\r\n]*)"
)


def _redact_multiline_assignment(match: re.Match[str]) -> str:
    return (
        f"{match.group('prefix')}{match.group('quote')}"
        f"[REDACTED]{match.group('quote')}"
    )


def redact_sensitive_text(value: str) -> str:
    """Replace high-confidence credentials while retaining bounded diagnostics."""

    result = value
    result = _SENSITIVE_MULTILINE_ASSIGNMENT_PATTERN.sub(
        _redact_multiline_assignment,
        result,
    )
    result = _SENSITIVE_ASSIGNMENT_PATTERN.sub(
        lambda match: f"{match.group('prefix')}[REDACTED]", result
    )
    for pattern in _SENSITIVE_PATTERNS:
        result = pattern.sub("[REDACTED]", result)
    return result


def is_sensitive_field_name(value: str) -> bool:
    """Return whether a mapping key describes credential material."""
    folded = value.casefold()
    return any(marker in folded for marker in _SENSITIVE_FIELD_MARKERS)


def redact_sensitive_payload(value: object) -> object:
    """Recursively redact strings and sensitive mapping values before persistence."""
    if isinstance(value, str):
        return redact_sensitive_text(value)
    if isinstance(value, dict):
        result: dict[str, object] = {}
        for key, item in cast(dict[str, object], value).items():
            if is_sensitive_field_name(key):
                result[key] = "[REDACTED]"
            else:
                result[key] = redact_sensitive_payload(item)
        return result
    if isinstance(value, (list, tuple)):
        items = cast(list[object] | tuple[object, ...], value)
        return [redact_sensitive_payload(item) for item in items]
    return value
