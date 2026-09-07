"""Regression checks for the P7-03 integrated delivery contract."""

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


def test_integrated_runbook_keeps_nanobot_as_the_only_user_entrypoint() -> None:
    runbook = (ROOT / "docs" / "runbooks" / "local-nanobot-devpilot.md").read_text(
        encoding="utf-8"
    )
    assert "uv sync --project .\\nanobot --locked" in runbook
    assert (
        "uv pip install --python .\\nanobot\\.venv\\Scripts\\python.exe -e .\\devpilot-agent"
        in runbook
    )
    assert "nanobot 自带 WebUI 是唯一用户入口" in runbook
    assert "不要启动 `web/`、Vite 或 18801 端口" in runbook
    assert "E2B_API_KEY" in runbook
    assert "local-data" in runbook and "删除" in runbook


def test_root_readme_points_to_the_integrated_environment() -> None:
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    assert "uv sync --project .\\nanobot --locked" in readme
    assert "devpilot-agent/docs/runbooks/local-nanobot-devpilot.md" in readme
