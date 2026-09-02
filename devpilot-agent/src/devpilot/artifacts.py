"""Atomic, server-owned Markdown Artifact storage."""

from __future__ import annotations

import hashlib
import os
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path

from .redaction import redact_sensitive_text

MAX_ARTIFACT_BYTES = 1 * 1024 * 1024
_SUPPORTED_MARKDOWN = frozenset(
    {"project-overview.md", "feature-delivery.md", "feature-delivery-failure.md"}
)


@dataclass(frozen=True)
class ArtifactWrite:
    path: Path
    sha256: str
    size_bytes: int


class ArtifactService:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).expanduser().resolve()

    def write_markdown(
        self, *, project_id: str, run_id: str, name: str, content: str
    ) -> ArtifactWrite:
        if name not in _SUPPORTED_MARKDOWN:
            raise ValueError(
                "only project-overview.md and feature-delivery.md are supported"
            )
        for value, label in ((project_id, "project_id"), (run_id, "run_id")):
            try:
                uuid.UUID(value)
            except (ValueError, AttributeError, TypeError) as exc:
                raise ValueError(f"{label} must be a UUID") from exc
        encoded = redact_sensitive_text(content).encode("utf-8")
        if len(encoded) > MAX_ARTIFACT_BYTES:
            raise ValueError("Artifact exceeds the 1 MiB limit")
        destination = self.root / "artifacts" / project_id / run_id / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256(encoded).hexdigest()
        with tempfile.NamedTemporaryFile(
            mode="wb", prefix=".artifact-", dir=destination.parent, delete=False
        ) as handle:
            temporary = Path(handle.name)
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.replace(temporary, destination)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
        return ArtifactWrite(path=destination, sha256=digest, size_bytes=len(encoded))
