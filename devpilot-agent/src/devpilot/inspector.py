"""Deterministic, read-only repository inspection for the first P1 Skill."""

from __future__ import annotations

import json
import os
import re
import tomllib
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, cast

from .paths import AllowedRoots

MAX_FILE_BYTES = 1 * 1024 * 1024
MAX_FILES = 20_000
MAX_METADATA_BYTES = 200 * 1024 * 1024

IGNORED_DIRS = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".venv",
        "venv",
        "node_modules",
        "dist",
        "build",
        "coverage",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        "local-data",
        "secrets",
    }
)
IGNORED_FILES = frozenset({".env", ".env.local", ".env.production", "credentials.json"})
SENSITIVE_SUFFIXES = frozenset({".pem", ".key", ".p12", ".pfx", ".crt"})


@dataclass(frozen=True)
class InspectionResult:
    project_path: str
    technology_stacks: list[str]
    entry_points: list[str]
    test_paths: list[str]
    dependency_files: list[str]
    config_files: list[str]
    readme_files: list[str]
    top_level_directories: list[str]
    evidence_files: list[str]
    files_seen: int
    metadata_bytes: int
    ignored_entries: int
    truncated: bool
    unknowns: list[str]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _relative(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def _is_sensitive_file(path: Path) -> bool:
    lowered = path.name.lower()
    return (
        lowered in IGNORED_FILES
        or path.suffix.lower() in SENSITIVE_SUFFIXES
        or "secret" in lowered
    )


def _read_text(path: Path, budget: int = MAX_FILE_BYTES) -> str | None:
    try:
        if path.stat().st_size > budget or _is_sensitive_file(path):
            return None
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _python_facts(
    root: Path, evidence: list[str], unknowns: list[str]
) -> tuple[list[str], list[str]]:
    entries: list[str] = []
    dependencies: list[str] = []
    pyproject = root / "pyproject.toml"
    if pyproject.exists():
        dependencies.append("pyproject.toml")
        evidence.append("pyproject.toml")
        content = _read_text(pyproject)
        if content:
            try:
                payload = tomllib.loads(content)
                project = cast(dict[str, Any], payload.get("project", {}))
                raw_scripts = project.get("scripts", {})
                scripts = cast(dict[str, Any], raw_scripts) if isinstance(raw_scripts, dict) else {}
                entries.extend(
                    f"pyproject.toml:[project.scripts].{name}"
                    for name in sorted(str(key) for key in scripts)
                )
            except tomllib.TOMLDecodeError:
                unknowns.append("pyproject.toml 解析失败")
    for name in ("requirements.txt", "setup.py", "setup.cfg", "Pipfile"):
        if (root / name).exists():
            dependencies.append(name)
            evidence.append(name)
    candidates = sorted(
        path
        for path in root.rglob("*.py")
        if (
            not any(part in IGNORED_DIRS for part in path.parts)
            and path.name in {"main.py", "app.py", "__main__.py"}
        )
    )
    entries.extend(_relative(path, root) for path in candidates[:20])
    return entries, dependencies


def _node_facts(
    root: Path, evidence: list[str], unknowns: list[str]
) -> tuple[list[str], list[str]]:
    entries: list[str] = []
    dependencies: list[str] = []
    package = root / "package.json"
    if not package.exists():
        return entries, dependencies
    dependencies.append("package.json")
    evidence.append("package.json")
    content = _read_text(package)
    if not content:
        unknowns.append("package.json 不可读取或超出大小限制")
        return entries, dependencies
    try:
        payload = json.loads(content)
    except json.JSONDecodeError:
        unknowns.append("package.json 解析失败")
        return entries, dependencies
    if isinstance(payload.get("main"), str):
        entries.append(f"package.json:main={payload['main']}")
    raw_scripts = payload.get("scripts", {})
    scripts = cast(dict[str, Any], raw_scripts) if isinstance(raw_scripts, dict) else {}
    if scripts:
        entries.extend(f"package.json:[scripts].{name}" for name in sorted(scripts))
    for name in ("package-lock.json", "pnpm-lock.yaml", "yarn.lock"):
        if (root / name).exists():
            dependencies.append(name)
            evidence.append(name)
    return entries, dependencies


def inspect_project(path: str | Path, allowed_roots: AllowedRoots) -> InspectionResult:
    """Collect bounded facts without opening secrets or executing repository code."""
    root = allowed_roots.require_project(path)
    technology: set[str] = set()
    entry_points: list[str] = []
    test_paths: list[str] = []
    dependency_files: list[str] = []
    config_files: list[str] = []
    readme_files: list[str] = []
    evidence_files: list[str] = []
    top_level_dirs: set[str] = set()
    unknowns: list[str] = []
    files_seen = 0
    metadata_bytes = 0
    ignored_entries = 0
    truncated = False

    if any(
        (root / marker).exists()
        for marker in ("pyproject.toml", "setup.py", "requirements.txt")
    ):
        technology.add("Python")
        python_entries, python_dependencies = _python_facts(root, evidence_files, unknowns)
        entry_points.extend(python_entries)
        dependency_files.extend(python_dependencies)
    if (root / "package.json").exists():
        technology.add("Node.js")
        node_entries, node_dependencies = _node_facts(root, evidence_files, unknowns)
        entry_points.extend(node_entries)
        dependency_files.extend(node_dependencies)

    for base, dir_names, file_names in os.walk(root, topdown=True, followlinks=False):
        base_path = Path(base)
        kept_dirs: list[str] = []
        for name in sorted(dir_names):
            child = base_path / name
            if name in IGNORED_DIRS or _is_sensitive_file(child):
                ignored_entries += 1
                continue
            kept_dirs.append(name)
            if base_path == root:
                top_level_dirs.add(name)
        dir_names[:] = kept_dirs
        for name in sorted(file_names):
            path_obj = base_path / name
            if _is_sensitive_file(path_obj):
                ignored_entries += 1
                continue
            if path_obj.is_symlink():
                ignored_entries += 1
                unknowns.append(f"符号链接未扫描: {_relative(path_obj, root)}")
                continue
            if files_seen >= MAX_FILES or metadata_bytes >= MAX_METADATA_BYTES:
                truncated = True
                break
            try:
                size = path_obj.stat().st_size
            except OSError:
                unknowns.append(f"无法读取元数据: {_relative(path_obj, root)}")
                continue
            files_seen += 1
            metadata_bytes += size
            relative = _relative(path_obj, root)
            lower_name = name.lower()
            if lower_name.startswith("readme"):
                readme_files.append(relative)
            if lower_name in {
                "pyproject.toml",
                "package.json",
                "tsconfig.json",
                "vite.config.ts",
                "webpack.config.js",
            }:
                config_files.append(relative)
            if re.search(r"(^|/)(tests?|__tests__)(/|$)", relative, re.IGNORECASE):
                test_paths.append(relative)
        if truncated:
            break

    if not technology:
        unknowns.append("未发现受支持的 Python 或 Node.js 项目标志文件")
    if not entry_points:
        unknowns.append("未发现确定的入口文件或项目脚本")
    if not test_paths:
        unknowns.append("未发现测试目录或测试文件")
    return InspectionResult(
        project_path=str(root),
        technology_stacks=sorted(technology),
        entry_points=sorted(set(entry_points)),
        test_paths=sorted(set(test_paths))[:100],
        dependency_files=sorted(set(dependency_files)),
        config_files=sorted(set(config_files)),
        readme_files=sorted(set(readme_files))[:20],
        top_level_directories=sorted(top_level_dirs),
        evidence_files=sorted(set(evidence_files)),
        files_seen=files_seen,
        metadata_bytes=metadata_bytes,
        ignored_entries=ignored_entries,
        truncated=truncated,
        unknowns=sorted(set(unknowns)),
    )
