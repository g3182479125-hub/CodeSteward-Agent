"""Local Skill validation and atomic publication; remote installation is out of scope."""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

REQUIRED_HEADINGS = (
    "## 适用场景",
    "## 禁用场景",
    "## 前置条件",
    "## 允许 Tool",
    "## 执行步骤",
    "## 输出契约",
)
_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{1,63}$")


@dataclass(frozen=True)
class SkillValidation:
    valid: bool
    name: str | None
    version: str | None
    status: str | None
    reviewed: bool
    errors: tuple[str, ...]


def _frontmatter(text: str) -> tuple[str, str] | None:
    if not text.startswith("---\n"):
        return None
    end = text.find("\n---", 4)
    if end < 0:
        return None
    return text[4:end], text[end + 4 :]


def validate_skill(path: str | Path) -> SkillValidation:
    errors: list[str] = []
    skill_path = Path(path).expanduser()
    try:
        text = skill_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return SkillValidation(False, None, None, None, False, ("SKILL.md cannot be read",))
    parsed = _frontmatter(text)
    if parsed is None:
        return SkillValidation(False, None, None, None, False, ("YAML frontmatter is required",))
    header, body = parsed
    values: dict[str, str] = {}
    for line in header.splitlines():
        if ":" in line and not line.startswith(" "):
            key, value = line.split(":", 1)
            values[key.strip()] = value.strip().strip('"\'')
    name = values.get("name")
    version_match = re.search(r"^\s+version:\s*([^\s]+)\s*$", header, re.MULTILINE)
    version = version_match.group(1) if version_match else values.get("version")
    if not name or not _NAME.fullmatch(name):
        errors.append("name must be a lowercase kebab-case value")
    if not version:
        errors.append("version is required")
    if not values.get("description"):
        errors.append("description is required")
    status_match = re.search(r"^\s+status:\s*([a-z-]+)\s*$", header, re.MULTILINE)
    status = status_match.group(1) if status_match else None
    if status not in {"draft", "reviewed", "enabled", "active", "disabled", "quarantined"}:
        errors.append("metadata.devpilot.status is invalid or missing")
    reviewed_match = re.search(r"^\s+reviewed:\s*(true|false)\s*$", header, re.MULTILINE)
    reviewed = reviewed_match is not None and reviewed_match.group(1) == "true"
    if not reviewed:
        errors.append("metadata.devpilot.reviewed must be true before publication")
    if "devpilot_inspect_project" not in header:
        errors.append("allowed_tools must include devpilot_inspect_project")
    for heading in REQUIRED_HEADINGS:
        if heading not in body:
            errors.append(f"missing required section: {heading}")
    return SkillValidation(not errors, name, version, status, reviewed, tuple(errors))


def publish_skill(source: str | Path, nanobot_workspace: str | Path) -> tuple[Path, str]:
    """Publish one reviewed/enabled catalog Skill using an atomic replacement."""
    source_path = Path(source).expanduser().resolve()
    result = validate_skill(source_path)
    if not result.valid or result.name is None:
        raise ValueError("Skill validation failed: " + "; ".join(result.errors))
    if result.status not in {"enabled", "active"}:
        raise ValueError("Skill must be enabled or active before publication")
    content = source_path.read_bytes()
    digest = hashlib.sha256(content).hexdigest()
    destination = (
        Path(nanobot_workspace).expanduser().resolve()
        / "skills"
        / result.name
        / "SKILL.md"
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="wb", prefix=".skill-", dir=destination.parent, delete=False
    ) as handle:
        temporary = Path(handle.name)
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
    try:
        os.replace(temporary, destination)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return destination, digest
