import asyncio

from nanobot.agent.memory import MemoryStore


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
