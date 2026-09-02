"""Shared redaction for data leaving the execution boundary."""

from __future__ import annotations

import re

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
