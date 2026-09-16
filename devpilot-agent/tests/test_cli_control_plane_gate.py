from __future__ import annotations

import pytest

from devpilot.cli import run_serve


def test_experimental_control_plane_is_disabled_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DEVPILOT_ENABLE_EXPERIMENTAL_CONTROL_PLANE", raising=False)

    assert run_serve(host="127.0.0.1", port=18800) == 2


def test_experimental_control_plane_still_rejects_non_loopback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEVPILOT_ENABLE_EXPERIMENTAL_CONTROL_PLANE", "1")

    assert run_serve(host="0.0.0.0", port=18800) == 2


def test_experimental_control_plane_requires_internal_auth_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEVPILOT_ENABLE_EXPERIMENTAL_CONTROL_PLANE", "1")
    monkeypatch.delenv("DEVPILOT_CONTROL_PLANE_TOKEN", raising=False)

    assert run_serve(host="127.0.0.1", port=18800) == 2
