"""Configuration helpers shared by Alembic and migration tests."""

from __future__ import annotations

import os
from collections.abc import Mapping

DEFAULT_DATABASE_URL = "sqlite:///local-data/devpilot.db"


def resolve_database_url(
    configured_url: str | None, environ: Mapping[str, str] | None = None
) -> str | None:
    """Use the process override only when Alembic still has its placeholder URL."""
    environment = os.environ if environ is None else environ
    if configured_url in {None, DEFAULT_DATABASE_URL}:
        return environment.get("DEVPILOT_DATABASE_URL", configured_url)
    return configured_url
