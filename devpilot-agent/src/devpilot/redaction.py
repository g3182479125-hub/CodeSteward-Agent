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
_ASSIGNMENT_KEY = (
    r"[\"']?(?:api[_-]?key|access[_-]?key|secret|token|password|passwd)"
    r"[\"']?"
)
_ASSIGNMENT_VALUE = (
    r'"""(?:\\.|[^"])*"""|'
    + "'''(?:\\\\.|[^'])*'''|"
    + r'"(?:\\.|[^"])*"|'
    + r"'(?:\\.|[^'])*'|"
    + r"[^\s,;]+"
)
_SENSITIVE_ASSIGNMENT_PATTERN = re.compile(
    rf"(?i)(?P<prefix>{_ASSIGNMENT_KEY}\s*[=:]\s*)"
    rf"(?P<value>{_ASSIGNMENT_VALUE})"
)


def redact_sensitive_text(value: str) -> str:
    """Replace high-confidence credentials while retaining bounded diagnostics."""

    result = value
    result = _SENSITIVE_ASSIGNMENT_PATTERN.sub(
        lambda match: f"{match.group('prefix')}[REDACTED]", result
    )
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
                for marker in (
                    "token",
                    "secret",
                    "password",
                    "passwd",
                    "api_key",
                    "apikey",
                    "access_key",
                    "authorization",
                )
            ):
                result[key] = "[REDACTED]"
            else:
                result[key] = redact_sensitive_payload(item)
        return result
    if isinstance(value, (list, tuple)):
        items = cast(list[object] | tuple[object, ...], value)
        return [redact_sensitive_payload(item) for item in items]
    return value
