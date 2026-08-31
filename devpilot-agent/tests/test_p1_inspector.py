import json
from pathlib import Path

import pytest

from devpilot.inspector import MAX_FILE_BYTES, inspect_project
from devpilot.paths import AllowedRoots, PathBoundaryError
from devpilot.report import render_project_overview

FIXTURES = Path(__file__).parent / "fixtures"


def test_python_fixture_reports_key_facts() -> None:
    result = inspect_project(FIXTURES / "python-project", AllowedRoots.from_paths([FIXTURES]))

    assert result.technology_stacks == ["Python"]
    assert "pyproject.toml:[project.scripts].fixture-python" in result.entry_points
    assert "tests/sample_checks.py" in result.test_paths
    assert "pyproject.toml" in result.dependency_files
    assert result.truncated is False


def test_node_fixture_reports_scripts_and_main() -> None:
    result = inspect_project(FIXTURES / "node-project", AllowedRoots.from_paths([FIXTURES]))

    assert result.technology_stacks == ["Node.js"]
    assert "package.json:main=src/index.js" in result.entry_points
    assert "package.json:[scripts].test" in result.entry_points
    assert "test/index.test.js" in result.test_paths


def test_outside_allowed_root_is_rejected() -> None:
    with pytest.raises(PathBoundaryError):
        inspect_project(FIXTURES.parent, AllowedRoots.from_paths([FIXTURES]))


def test_sensitive_and_large_files_are_not_model_facts(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    (root / ".env").write_text("DEEPSEEK_API_KEY=not-for-output", encoding="utf-8")
    (root / "large.txt").write_bytes(b"x" * (MAX_FILE_BYTES + 1))
    result = inspect_project(root, AllowedRoots.from_paths([tmp_path]))

    assert ".env" not in result.evidence_files
    assert result.metadata_bytes >= MAX_FILE_BYTES + 1
    assert "DEEPSEEK_API_KEY" not in json.dumps(result.to_dict())


def test_report_has_fixed_sections_and_unknowns() -> None:
    result = inspect_project(FIXTURES / "node-project", AllowedRoots.from_paths([FIXTURES]))
    report = render_project_overview(result)

    sections = (
        "技术栈", "入口", "目录", "运行方式", "测试", "依赖", "风险", "未知项", "证据文件"
    )
    for section in sections:
        assert f"## {section}" in report
