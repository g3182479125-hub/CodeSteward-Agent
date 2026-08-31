"""Filesystem boundary helpers shared by P1 read-only services."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


class PathBoundaryError(ValueError):
    """Raised when a requested path is outside the configured real roots."""


def real_path(path: str | Path, *, must_exist: bool = True) -> Path:
    """Expand and resolve a path, following links before policy checks."""
    candidate = Path(path).expanduser()
    try:
        return candidate.resolve(strict=must_exist)
    except (OSError, RuntimeError) as exc:
        raise PathBoundaryError(f"path cannot be resolved: {candidate}") from exc


@dataclass(frozen=True)
class AllowedRoots:
    """Allow only existing directories inside configured real paths."""

    roots: tuple[Path, ...]

    @classmethod
    def from_paths(cls, paths: list[str | Path] | tuple[str | Path, ...]) -> AllowedRoots:
        resolved: list[Path] = []
        for path in paths:
            root = real_path(path)
            if not root.is_dir():
                raise PathBoundaryError(f"allowed root is not a directory: {root}")
            if root not in resolved:
                resolved.append(root)
        return cls(tuple(resolved))

    def contains(self, path: str | Path) -> bool:
        try:
            candidate = real_path(path)
        except PathBoundaryError:
            return False
        return any(candidate == root or candidate.is_relative_to(root) for root in self.roots)

    def require_project(self, path: str | Path) -> Path:
        candidate = real_path(path)
        if not candidate.is_dir():
            raise PathBoundaryError(f"project is not a directory: {candidate}")
        if not any(candidate == root or candidate.is_relative_to(root) for root in self.roots):
            raise PathBoundaryError(f"project is outside allowed roots: {candidate}")
        return candidate
