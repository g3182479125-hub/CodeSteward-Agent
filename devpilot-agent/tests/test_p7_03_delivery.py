"""Regression checks for the P7-03 integrated delivery contract."""

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).parents[1]
REPO_ROOT = ROOT.parent


def test_devpilot_declares_nanobot_tools() -> None:
    with (ROOT / "pyproject.toml").open("rb") as handle:
        project = tomllib.load(handle)["project"]

    entry_points = project["entry-points"]["nanobot.tools"]
    assert len(entry_points) == 14
    assert "devpilot_inspect_project" in entry_points
    assert "devpilot_confirmed_action" in entry_points
    assert "uvicorn>=0.34,<1.0" in project["dependencies"]
    assert "uvicorn[standard]>=0.34,<1.0" not in project["dependencies"]


def test_integrated_runbook_keeps_nanobot_as_the_only_user_entrypoint() -> None:
    runbook = (ROOT / "docs" / "runbooks" / "local-nanobot-devpilot.md").read_text(
        encoding="utf-8"
    )
    assert "install-integrated.ps1" in runbook
    installer = (ROOT / "scripts" / "install-integrated.ps1").read_text(encoding="utf-8")
    assert "uv sync --project $nanobotRoot --locked" in installer
    assert "uv export --project $devpilotRoot --locked" in installer
    assert "nanobot 自带 WebUI 是唯一用户入口" in runbook
    assert "不要启动 `web/`、Vite 或 18801 端口" in runbook
    assert "DEVPILOT_DATABASE_URL" in runbook
    assert "E2B_API_KEY" in runbook
    assert "local-data" in runbook and "删除" in runbook


def test_root_readme_points_to_the_integrated_environment() -> None:
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    assert "scripts\\install-integrated.ps1" in readme
    assert "devpilot-agent/docs/runbooks/local-nanobot-devpilot.md" in readme


def test_p7_handoff_contains_source_boundaries_without_credentials() -> None:
    handoff = (ROOT / "docs" / "handoffs" / "2026-09-07-p7-delivery.md").read_text(
        encoding="utf-8"
    )
    assert "P7-01、P7-02、P7-03 已完成" in handoff
    assert "DeepSeek Harness" in handoff
    assert "Codex Harness" in handoff
    assert "DEVPILOT_DATABASE_URL" in handoff
    assert "14 个 `devpilot_*` entry points" in handoff
    assert re.search(r"e2b_[a-f0-9]{20,}", handoff.lower()) is None
    assert "sk-" not in handoff.lower()
