import asyncio
from types import SimpleNamespace

from nanobot.agent.memory import MemoryStore
from nanobot.agent.tools.filesystem import ReadFileTool, WriteFileTool
from nanobot.agent.tools.shell import ExecTool
from nanobot.security.workspace_access import (
    bind_workspace_scope,
    build_workspace_scope,
    reset_workspace_scope,
)


def test_dream_cannot_write_trial_bug_fix_skill_through_any_file_writer(tmp_path) -> None:
    store = MemoryStore(tmp_path)
    tools = store.build_dream_tools()
    attempts = [
        (
            "write_file",
            {"path": "skills/bug-fix/SKILL.md", "content": "attempt"},
        ),
        (
            "edit_file",
            {
                "path": "skills/bug-fix/SKILL.md",
                "old_text": "old",
                "new_text": "attempt",
            },
        ),
        (
            "apply_patch",
            {
                "edits": [
                    {
                        "path": "skills/bug-fix/../bug-fix/SKILL.md",
                        "action": "add",
                        "new_text": "attempt",
                    }
                ]
            },
        ),
    ]

    for name, params in attempts:
        result = asyncio.run(tools.execute(name, params))
        assert getattr(result, "is_error", False) is True
        assert "blocked" in str(result).lower()
    assert not (tmp_path / "skills" / "bug-fix" / "SKILL.md").exists()


def test_dream_can_still_write_an_ordinary_workspace_skill(tmp_path) -> None:
    store = MemoryStore(tmp_path)
    result = asyncio.run(
        store.build_dream_tools().execute(
            "write_file",
            {"path": "skills/ordinary/SKILL.md", "content": "ordinary"},
        )
    )
    assert getattr(result, "is_error", False) is False
    assert (tmp_path / "skills" / "ordinary" / "SKILL.md").read_text() == "ordinary"


def test_main_filesystem_guard_and_controlled_shell_boundary(tmp_path, monkeypatch) -> None:
    trial_dir = tmp_path / "skills" / "bug-fix"
    writer = WriteFileTool(
        workspace=tmp_path,
        allowed_dir=tmp_path,
        blocked_write_dirs=[trial_dir],
    )
    result = asyncio.run(
        writer.execute(path="skills/bug-fix/SKILL.md", content="attempt")
    )
    assert getattr(result, "is_error", False) is True
    assert not (trial_dir / "SKILL.md").exists()

    config = SimpleNamespace(exec=SimpleNamespace(enable=True))
    assert ExecTool.enabled(SimpleNamespace(config=config)) is True
    monkeypatch.setenv("DEVPILOT_ENABLE_SKILL_IMPROVEMENT", "1")
    assert ExecTool.enabled(SimpleNamespace(config=config)) is False


def test_main_filesystem_guard_follows_selected_project_scope(tmp_path) -> None:
    selected_project = tmp_path / "selected-project"
    selected_project.mkdir()
    writer = WriteFileTool(workspace=tmp_path, allowed_dir=tmp_path)
    scope = build_workspace_scope(selected_project, "full", source_channel="websocket")
    token = bind_workspace_scope(scope)
    try:
        result = asyncio.run(
            writer.execute(
                path="skills/bug-fix/SKILL.md",
                content="attempt",
            )
        )
    finally:
        reset_workspace_scope(token)
    assert getattr(result, "is_error", False) is True
    assert not (selected_project / "skills" / "bug-fix" / "SKILL.md").exists()


def test_main_filesystem_guard_hides_private_skill_improvement_data(tmp_path) -> None:
    private_dir = tmp_path / "private-data"
    private_dir.mkdir()
    (private_dir / "candidate.json").write_text("secret", encoding="utf-8")
    reader = ReadFileTool(
        workspace=tmp_path,
        allowed_dir=tmp_path,
        blocked_read_dirs=[private_dir],
    )
    result = asyncio.run(reader.execute(path="private-data/candidate.json"))
    assert getattr(result, "is_error", False) is True
    assert "private data" in str(result).lower()

    writer = WriteFileTool(
        workspace=tmp_path,
        allowed_dir=tmp_path,
        blocked_write_dirs=[private_dir],
    )
    write_result = asyncio.run(
        writer.execute(path="private-data/devpilot.db", content="should not exist")
    )
    assert getattr(write_result, "is_error", False) is True
    assert not (private_dir / "devpilot.db").exists()


def test_dream_hides_configured_private_root_and_integrity_key(tmp_path, monkeypatch) -> None:
    workspace = tmp_path / "workspace"
    private_dir = workspace / "private-data"
    private_key = workspace / "private.key"
    private_dir.mkdir(parents=True)
    private_key.write_text("secret", encoding="utf-8")
    (private_dir / "candidate.json").write_text("secret", encoding="utf-8")
    monkeypatch.setenv("DEVPILOT_DATA_DIR", str(private_dir))
    monkeypatch.setenv("DEVPILOT_INTEGRITY_KEY_FILE", str(private_key))

    tools = MemoryStore(workspace).build_dream_tools()
    for name, params in (
        ("read_file", {"path": "private-data/candidate.json"}),
        ("write_file", {"path": "private-data/new.json", "content": "no"}),
        ("write_file", {"path": "private.key", "content": "no"}),
    ):
        result = asyncio.run(tools.execute(name, params))
        assert getattr(result, "is_error", False) is True
        assert "private" in str(result).lower()


def test_p8_tools_are_not_available_without_controlled_opt_in(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("DEVPILOT_ENABLE_SKILL_IMPROVEMENT", raising=False)
    from devpilot.nanobot_tools import SkillCandidateTool, SkillFeedbackTool

    assert SkillCandidateTool.enabled(SimpleNamespace()) is False
    assert SkillFeedbackTool.enabled(SimpleNamespace()) is False
    assert not MemoryStore(tmp_path).build_dream_tools().has("devpilot_skill_candidate")
