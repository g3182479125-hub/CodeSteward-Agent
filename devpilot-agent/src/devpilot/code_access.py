"""Bounded, read-only source search and file access for P2A-02."""

from __future__ import annotations

import fnmatch
import hashlib
import os
import re
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from .inspector import IGNORED_DIRS, IGNORED_FILES, SENSITIVE_SUFFIXES
from .paths import AllowedRoots, PathBoundaryError
from .storage import ProjectStore

MAX_QUERY_CHARS = 256
MAX_SEARCH_MATCHES = 200
MAX_SEARCH_FILES = 20_000
MAX_SOURCE_FILE_BYTES = 1 * 1024 * 1024
MAX_READ_BYTES = 256 * 1024
MAX_READ_LINES = 2_000
MAX_MATCH_CHARS = 500

_WINDOWS_RESERVED = frozenset(
    {"con", "prn", "aux", "nul", "clock$"}
    | {f"com{index}" for index in range(1, 10)}
    | {f"lpt{index}" for index in range(1, 10)}
)
_IGNORED_DIRS = frozenset(item.casefold() for item in IGNORED_DIRS)
_IGNORED_FILES = frozenset(item.casefold() for item in IGNORED_FILES)
_SENSITIVE_SUFFIXES = frozenset(item.casefold() for item in SENSITIVE_SUFFIXES)
_SECRET_NAME = re.compile(
    r"(?i)^(?:\.env(?:\..*)?|"
    r"(?:.*[._-])?(?:secret|credential|password|api[_-]?key|access[_-]?token|auth[_-]?token)"
    r"(?:[._-].*)?)$"
)
_SECRET_VALUE_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b"),
    re.compile(r"(?i)(authorization\s*[:=]\s*bearer\s+)\S+"),
    re.compile(r"(?i)((?:api[_-]?key|secret|password|token)\s*[:=]\s*)['\"]?[^'\"\s,;]+"),
)


class CodeAccessError(ValueError):
    """Stable, user-safe rejection from the read-only code boundary."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True, slots=True)
class SearchMatch:
    path: str
    line: int
    text: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class SearchResult:
    matches: tuple[SearchMatch, ...]
    files_scanned: int
    truncated: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "matches": [item.to_dict() for item in self.matches],
            "files_scanned": self.files_scanned,
            "truncated": self.truncated,
        }


@dataclass(frozen=True, slots=True)
class ReadResult:
    path: str
    content: str
    sha256: str
    start_line: int
    end_line: int
    truncated: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _validate_component(component: str) -> None:
    if not component or component in {".", ".."}:
        raise CodeAccessError("PATH_INVALID", "path contains an invalid component")
    if component != component.rstrip(" ."):
        raise CodeAccessError("WINDOWS_PATH_ALIAS", "trailing dots or spaces are not accepted")
    if ":" in component:
        raise CodeAccessError("WINDOWS_PATH_ALIAS", "NTFS stream and drive syntax are not accepted")
    stem = component.split(".", 1)[0].casefold()
    if stem in _WINDOWS_RESERVED:
        raise CodeAccessError(
            "WINDOWS_PATH_ALIAS", "reserved Windows device names are not accepted"
        )


def _relative_parts(raw_path: str) -> tuple[str, ...]:
    if not raw_path or "\x00" in raw_path:
        raise CodeAccessError("PATH_INVALID", "path is empty or contains NUL")
    posix = PurePosixPath(raw_path.replace("\\", "/"))
    windows = PureWindowsPath(raw_path)
    if posix.is_absolute() or windows.is_absolute() or windows.drive:
        raise CodeAccessError("PATH_NOT_RELATIVE", "only project-relative paths are accepted")
    parts = tuple(part for part in posix.parts if part != ".")
    if not parts or ".." in parts:
        raise CodeAccessError("PATH_OUTSIDE_PROJECT", "path traversal is not accepted")
    for part in parts:
        _validate_component(part)
    return parts


def _is_forbidden(parts: tuple[str, ...]) -> bool:
    for part in parts:
        folded = part.casefold()
        if folded in _IGNORED_DIRS:
            return True
        if folded in _IGNORED_FILES:
            return True
        if Path(part).suffix.casefold() in _SENSITIVE_SUFFIXES:
            return True
        if _SECRET_NAME.fullmatch(part):
            return True
    return False


def _target(project: Path, relative_path: str) -> tuple[Path, str]:
    parts = _relative_parts(relative_path)
    if _is_forbidden(parts):
        raise CodeAccessError("FORBIDDEN_FILE", "sensitive or generated paths are not readable")
    target = project.joinpath(*parts)
    try:
        resolved = target.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise CodeAccessError("FILE_NOT_FOUND", "requested file does not exist") from exc
    try:
        resolved.relative_to(project)
    except ValueError as exc:
        raise CodeAccessError("PATH_OUTSIDE_PROJECT", "resolved path escapes the project") from exc
    if not resolved.is_file() or target.is_symlink():
        raise CodeAccessError("TARGET_NOT_FILE", "requested path is not a regular file")
    return resolved, "/".join(parts)


def _read_source(path: Path) -> bytes:
    try:
        size = path.stat().st_size
        if size > MAX_SOURCE_FILE_BYTES:
            raise CodeAccessError("FILE_LIMIT_EXCEEDED", "source file exceeds the 1 MiB limit")
        content = path.read_bytes()
    except CodeAccessError:
        raise
    except OSError as exc:
        raise CodeAccessError("FILE_READ_FAILED", "source file could not be read") from exc
    if b"\x00" in content:
        raise CodeAccessError("BINARY_FILE_FORBIDDEN", "binary files are not readable")
    try:
        content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CodeAccessError(
            "BINARY_FILE_FORBIDDEN", "only UTF-8 source files are readable"
        ) from exc
    return content


def _redact(text: str) -> str:
    redacted = text
    for pattern in _SECRET_VALUE_PATTERNS:
        if pattern.groups:
            redacted = pattern.sub(r"\1[REDACTED]", redacted)
        else:
            redacted = pattern.sub("[REDACTED]", redacted)
    return redacted


def _validate_glob(pattern: str | None) -> str:
    if pattern is None or not pattern.strip():
        return "*"
    candidate = pattern.replace("\\", "/")
    if len(candidate) > 256 or "\x00" in candidate:
        raise CodeAccessError("GLOB_INVALID", "glob is empty or too long")
    windows = PureWindowsPath(candidate)
    if (
        candidate.startswith("/")
        or windows.is_absolute()
        or windows.drive
        or ".." in candidate.split("/")
    ):
        raise CodeAccessError("GLOB_INVALID", "glob must stay inside the project")
    return candidate


class CodeAccessService:
    """Resolve a registered project and expose bounded literal search/read operations."""

    def __init__(self, *, allowed_roots: AllowedRoots, store: ProjectStore) -> None:
        self.allowed_roots = allowed_roots
        self.store = store

    def _project(self, project_id: str) -> Path:
        if not project_id:
            raise CodeAccessError("PROJECT_ID_REQUIRED", "project_id is required")
        try:
            real_path = self.store.get_project_path(project_id)
            return self.allowed_roots.require_project(real_path)
        except (KeyError, PathBoundaryError) as exc:
            raise CodeAccessError(
                "PROJECT_NOT_AVAILABLE", "project is unknown or outside allowed roots"
            ) from exc

    def read(
        self,
        project_id: str,
        relative_path: str,
        *,
        start_line: int = 1,
        end_line: int | None = None,
        evidence_run_id: str | None = None,
    ) -> ReadResult:
        if start_line < 1:
            raise CodeAccessError("LINE_RANGE_INVALID", "start_line must be at least 1")
        if end_line is not None and (
            end_line < start_line or end_line - start_line + 1 > MAX_READ_LINES
        ):
            raise CodeAccessError(
                "LINE_RANGE_INVALID", "line range is invalid or exceeds 2,000 lines"
            )
        project = self._project(project_id)
        if evidence_run_id is not None:
            try:
                self.store.validate_open_run(project_id=project_id, run_id=evidence_run_id)
            except (KeyError, ValueError) as exc:
                raise CodeAccessError(
                    "EVIDENCE_RUN_INVALID", "evidence_run_id must be an open run for this project"
                ) from exc
        target, normalized = _target(project, relative_path)
        content = _read_source(target)
        lines = content.decode("utf-8").splitlines(keepends=True)
        requested_end = end_line or min(len(lines), start_line + MAX_READ_LINES - 1)
        selected = "".join(lines[start_line - 1 : requested_end])
        encoded = selected.encode("utf-8")
        truncated = end_line is None and requested_end < len(lines)
        if len(encoded) > MAX_READ_BYTES:
            encoded = encoded[:MAX_READ_BYTES]
            while encoded:
                try:
                    selected = encoded.decode("utf-8")
                    break
                except UnicodeDecodeError:
                    encoded = encoded[:-1]
            truncated = True
        selected = _redact(selected)
        actual_lines = selected.splitlines()
        actual_end = start_line + len(actual_lines) - 1 if actual_lines else start_line - 1
        result = ReadResult(
            path=normalized,
            content=selected,
            sha256=hashlib.sha256(content).hexdigest(),
            start_line=start_line,
            end_line=actual_end,
            truncated=truncated,
        )
        if evidence_run_id is not None:
            self.store.append_event(
                evidence_run_id,
                "evidence.read.completed",
                {
                    "project_id": project_id,
                    "path": result.path,
                    "start_line": result.start_line,
                    "end_line": result.end_line,
                    "truncated": result.truncated,
                    "sha256": result.sha256,
                },
            )
        return result

    def search(
        self,
        project_id: str,
        query: str,
        *,
        glob: str | None = None,
        limit: int = 50,
        case_sensitive: bool = False,
        evidence_run_id: str | None = None,
    ) -> SearchResult:
        if not query or len(query) > MAX_QUERY_CHARS or "\x00" in query:
            raise CodeAccessError("QUERY_INVALID", "query must contain 1–256 safe characters")
        if not 1 <= limit <= MAX_SEARCH_MATCHES:
            raise CodeAccessError("LIMIT_INVALID", "limit must be between 1 and 200")
        pattern = _validate_glob(glob)
        project = self._project(project_id)
        if evidence_run_id is not None:
            try:
                self.store.validate_open_run(project_id=project_id, run_id=evidence_run_id)
            except (KeyError, ValueError) as exc:
                raise CodeAccessError(
                    "EVIDENCE_RUN_INVALID", "evidence_run_id must be an open run for this project"
                ) from exc
        needle = query if case_sensitive else query.casefold()
        matches: list[SearchMatch] = []
        files_scanned = 0
        truncated = False
        for base, dir_names, file_names in os.walk(project, topdown=True, followlinks=False):
            base_path = Path(base)
            dir_names[:] = sorted(
                name
                for name in dir_names
                if not _is_forbidden((name,)) and not (base_path / name).is_symlink()
            )
            for name in sorted(file_names):
                path = base_path / name
                try:
                    relative = path.relative_to(project).as_posix()
                    parts = _relative_parts(relative)
                    if _is_forbidden(parts) or path.is_symlink():
                        continue
                    matches_glob = fnmatch.fnmatchcase(relative, pattern) or PurePosixPath(
                        relative
                    ).match(pattern)
                    if not matches_glob:
                        continue
                    if files_scanned >= MAX_SEARCH_FILES:
                        truncated = True
                        break
                    content = _read_source(path).decode("utf-8")
                except CodeAccessError:
                    continue
                files_scanned += 1
                for line_number, line in enumerate(content.splitlines(), start=1):
                    haystack = line if case_sensitive else line.casefold()
                    if needle in haystack:
                        matches.append(
                            SearchMatch(
                                path=relative,
                                line=line_number,
                                text=_redact(line[:MAX_MATCH_CHARS]),
                            )
                        )
                        if len(matches) >= limit:
                            truncated = True
                            break
                if len(matches) >= limit:
                    break
            if truncated:
                break
        result = SearchResult(tuple(matches), files_scanned, truncated)
        if evidence_run_id is not None:
            self.store.append_event(
                evidence_run_id,
                "evidence.search.completed",
                {
                    "project_id": project_id,
                    "query": query,
                    "glob": pattern,
                    "limit": limit,
                    "case_sensitive": case_sensitive,
                    "match_count": len(result.matches),
                    "files_scanned": result.files_scanned,
                    "truncated": result.truncated,
                    "matches": [
                        {"path": item.path, "line": item.line} for item in result.matches
                    ],
                },
            )
        return result
