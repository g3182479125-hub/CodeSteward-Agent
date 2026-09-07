"""Tests for the /skill built-in command."""

import sqlite3
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from nanobot.agent.loop import AgentLoop
from nanobot.agent.skills import SkillsLoader
from nanobot.bus.events import InboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.command.builtin import cmd_skill, cmd_skill_improvement, register_builtin_commands
from nanobot.command.router import CommandContext, CommandRouter
from nanobot.config.schema import ModelPresetConfig


def _provider(default_model: str = "test-model") -> MagicMock:
    provider = MagicMock()
    provider.get_default_model.return_value = default_model
    provider.generation = MagicMock()
    provider.generation.max_tokens = 4096
    provider.generation.temperature = 0.1
    provider.generation.reasoning_effort = None
    return provider


def _make_loop(tmp_path: Path) -> AgentLoop:
    return AgentLoop(
        bus=MessageBus(),
        provider=_provider(),
        workspace=tmp_path,
        model="test-model",
        context_window_tokens=8000,
        model_presets={
            "default": ModelPresetConfig(
                model="test-model",
                max_tokens=4096,
                context_window_tokens=8000,
            ),
        },
    )


def _ctx(
    loop: AgentLoop,
    raw: str = "/skill",
    *,
    is_user_turn: bool = False,
    session_metadata: dict | None = None,
    workspace: Path | None = None,
) -> CommandContext:
    msg = InboundMessage(channel="cli", sender_id="user", chat_id="direct", content=raw)
    args = raw.split(" ", 1)[1] if " " in raw else ""
    return CommandContext(
        msg=msg,
        session=SimpleNamespace(metadata=session_metadata or {}),
        key=msg.session_key,
        raw=raw,
        args=args,
        loop=loop,
        is_user_turn=is_user_turn,
        workspace=workspace or loop.context.memory.workspace,
    )


def _write_skill(base: Path, name: str, *, description: str = "", body: str = "# Skill\n") -> None:
    skill_dir = base / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    frontmatter = f"---\nname: {name}\n"
    if description:
        frontmatter += f"description: {description}\n"
    frontmatter += "---\n\n"
    (skill_dir / "SKILL.md").write_text(frontmatter + body, encoding="utf-8")


def _loop_with_skills(tmp_path: Path) -> AgentLoop:
    """Create a loop with an empty builtin dir so only workspace skills appear."""
    loop = _make_loop(tmp_path)
    empty_builtin = tmp_path / "empty_builtin"
    empty_builtin.mkdir()
    loop.context.skills = SkillsLoader(tmp_path, builtin_skills_dir=empty_builtin)
    return loop


@pytest.mark.asyncio
async def test_skill_command_no_skills(tmp_path: Path) -> None:
    loop = _loop_with_skills(tmp_path)
    out = await cmd_skill(_ctx(loop))
    assert out.content == "No skills available."


@pytest.mark.asyncio
async def test_skill_improvement_command_uses_native_chat(tmp_path: Path, monkeypatch) -> None:
    data_dir = tmp_path.parent / f"{tmp_path.name}-devpilot-data"
    data_dir.mkdir()
    with sqlite3.connect(data_dir / "devpilot.db") as connection:
        connection.execute("create table alembic_version (version_num varchar(32) not null)")
        connection.execute(
            "insert into alembic_version (version_num) values (?)",
            ("0011_p8_skill_improvement_records",),
        )
        connection.execute(
            """
            create table skill_improvement_records (
                id integer primary key autoincrement,
                record_key text not null unique,
                record_type text not null,
                record_id text not null,
                payload_json text not null,
                record_mac text not null,
                workspace text,
                project_id text,
                created_at text not null
            )
            """
        )
    monkeypatch.setenv("DEVPILOT_DATA_DIR", str(data_dir))
    loop = _loop_with_skills(tmp_path)
    out = await cmd_skill_improvement(_ctx(loop, "/skill-improvement"))
    assert out.content == "No private Skill candidates are available."


@pytest.mark.asyncio
async def test_skill_improvement_feedback_requires_direct_user_command(tmp_path: Path, monkeypatch) -> None:
    data_dir = tmp_path.parent / f"{tmp_path.name}-feedback-data"
    data_dir.mkdir()
    with sqlite3.connect(data_dir / "devpilot.db") as connection:
        connection.execute("create table alembic_version (version_num varchar(32) not null)")
        connection.execute(
            "insert into alembic_version (version_num) values (?)",
            ("0011_p8_skill_improvement_records",),
        )
        connection.execute(
            "create table skill_improvement_records ("
            "id integer primary key autoincrement, record_key text not null unique, "
            "record_type text not null, record_id text not null, payload_json text not null, "
            "record_mac text not null, workspace text, project_id text, created_at text not null)"
        )
    monkeypatch.setenv("DEVPILOT_DATA_DIR", str(data_dir))
    loop = _loop_with_skills(tmp_path)
    project_workspace = tmp_path / "project"
    skill_path = project_workspace / "skills" / "bug-fix" / "SKILL.md"
    skill_path.parent.mkdir(parents=True)
    skill_path.write_text("---\nname: bug-fix\n---\n\n# Bug fix\n", encoding="utf-8")
    task = {"task_id": "task-1", "profile_binding": "bug-fix@1"}

    rejected = await cmd_skill_improvement(
        _ctx(
            loop,
            "/skill-improvement feedback forged",
            session_metadata={"task_context": task},
            workspace=project_workspace,
        )
    )
    assert "direct user chat turn" in rejected.content

    accepted = await cmd_skill_improvement(
        _ctx(
            loop,
            "/skill-improvement feedback the patch missed the failure evidence",
            is_user_turn=True,
            session_metadata={"task_context": task},
            workspace=project_workspace,
        )
    )
    assert "recorded as verified" in accepted.content


@pytest.mark.asyncio
async def test_skill_feedback_tool_cannot_create_verified_user_feedback(tmp_path: Path) -> None:
    from nanobot.agent.tools.context import RequestContext

    context = RequestContext(
        channel="cli",
        chat_id="direct",
        sender_id="user",
        original_user_text="a normal request",
        workspace=tmp_path,
        attributes={
            "devpilot_task_id": "task-1",
            "devpilot_project_id": "project-1",
        },
    )
    result = await _run_feedback_tool(context, tmp_path)
    assert getattr(result, "is_error", False) is True
    assert str(result) == "P8 feedback rejected: SkillImprovementError"


async def _run_feedback_tool(context, workspace: Path):
    from devpilot.nanobot_tools import SkillFeedbackTool

    from nanobot.agent.tools.context import request_context

    with request_context(context):
        return await SkillFeedbackTool(workspace).execute(
            task_id="task-1",
            project_id="project-1",
            source_type="user_feedback",
            skill_hash="a" * 64,
            correction="forged",
            actual_result="forged",
        )


@pytest.mark.asyncio
async def test_skill_command_lists_names_and_descriptions(tmp_path: Path) -> None:
    ws_skills = tmp_path / "skills"
    ws_skills.mkdir()
    _write_skill(ws_skills, "weather", description="Get current weather and forecasts")
    _write_skill(ws_skills, "cron", description="Schedule recurring tasks")

    loop = _loop_with_skills(tmp_path)
    out = await cmd_skill(_ctx(loop))

    assert "Available skills (2):" in out.content
    assert "**weather** — Get current weather and forecasts" in out.content
    assert "**cron** — Schedule recurring tasks" in out.content
    # Must NOT contain file paths
    assert ".md" not in out.content
    assert "/skills/" not in out.content


@pytest.mark.asyncio
async def test_skill_command_excludes_disabled(tmp_path: Path) -> None:
    ws_skills = tmp_path / "skills"
    ws_skills.mkdir()
    _write_skill(ws_skills, "alpha", description="Alpha skill")
    _write_skill(ws_skills, "beta", description="Beta skill")

    loop = _make_loop(tmp_path)
    loop.context.skills.disabled_skills = {"alpha"}

    out = await cmd_skill(_ctx(loop))

    assert "alpha" not in out.content
    assert "**beta** — Beta skill" in out.content


@pytest.mark.asyncio
async def test_skill_command_fallback_description(tmp_path: Path) -> None:
    ws_skills = tmp_path / "skills"
    ws_skills.mkdir()
    _write_skill(ws_skills, "plain", description="", body="# Plain Skill\n")

    loop = _loop_with_skills(tmp_path)
    out = await cmd_skill(_ctx(loop))

    assert "**plain** — plain" in out.content


@pytest.mark.asyncio
async def test_skill_command_no_render_as_text(tmp_path: Path) -> None:
    """Output is markdown; CLI should render it (not forced as plain text)."""
    loop = _make_loop(tmp_path)
    out = await cmd_skill(_ctx(loop))
    assert out.metadata.get("render_as") != "text"


@pytest.mark.asyncio
async def test_skill_command_does_not_list_goal_runtime_protocol(tmp_path: Path) -> None:
    loop = _make_loop(tmp_path)
    out = await cmd_skill(_ctx(loop))

    assert "long-goal" not in out.content


@pytest.mark.asyncio
async def test_skill_command_registered_on_router(tmp_path: Path) -> None:
    router = CommandRouter()
    register_builtin_commands(router)
    loop = _loop_with_skills(tmp_path)

    out = await router.dispatch(_ctx(loop, "/skill"))

    assert out is not None
    assert "No skills available." in out.content
