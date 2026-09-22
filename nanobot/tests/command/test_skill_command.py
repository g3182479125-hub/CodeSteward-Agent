"""Tests for the /skill built-in command."""

import sqlite3
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from nanobot.agent.loop import AgentLoop
from nanobot.agent.skills import SkillsLoader
from nanobot.bus.events import InboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.command.builtin import (
    BUILTIN_COMMAND_SPECS,
    _skill_exception_label,
    _skill_status_label,
    cmd_skill,
    cmd_skill_improvement,
    register_builtin_commands,
)
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


@pytest.mark.parametrize(
    ("status", "label"),
    [
        ("active", "已启用"),
        ("evaluation_failed", "评测失败"),
        ("pending_confirmation", "等待确认"),
        ("stale", "已过期"),
        ("no_new_evidence", "没有新证据"),
        ("eligible", "符合启用条件"),
        ("no_verified_improvement", "未发现已验证改进"),
    ],
)
def test_skill_statuses_are_translated_for_native_chat(status: str, label: str) -> None:
    assert _skill_status_label(status) == label


def test_skill_status_unknown_value_has_chinese_fallback() -> None:
    assert _skill_status_label("unexpected_state") == "未识别状态（unexpected_state）"


def test_skill_exception_labels_are_chinese() -> None:
    assert _skill_exception_label(ValueError("bad input")) == "参数或状态不合法"
    assert _skill_exception_label(RuntimeError("unexpected")) == "内部处理失败"


def test_skill_improvement_command_metadata_is_chinese() -> None:
    spec = next(item for item in BUILTIN_COMMAND_SPECS if item.command == "/skill-improvement")
    assert spec.title == "审查 Skill 改进"
    assert "查看 Skill 候选" in spec.description


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
    monkeypatch.setenv("DEVPILOT_ENABLE_SKILL_IMPROVEMENT", "1")
    loop = _loop_with_skills(tmp_path)
    out = await cmd_skill_improvement(_ctx(loop, "/skill-improvement"))
    assert out.content == "暂无私有 Skill 候选。"


@pytest.mark.asyncio
async def test_skill_improvement_schedule_is_explicit_and_reuses_native_cron(
    tmp_path: Path, monkeypatch
) -> None:
    data_dir = tmp_path.parent / f"{tmp_path.name}-scheduled-data"
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
    skill_path = tmp_path / "skills" / "bug-fix" / "SKILL.md"
    skill_path.parent.mkdir(parents=True)
    skill_path.write_text("---\nname: bug-fix\n---\n\n# Bug fix\n", encoding="utf-8")
    monkeypatch.setenv("DEVPILOT_DATA_DIR", str(data_dir))
    monkeypatch.setenv("DEVPILOT_ENABLE_SKILL_IMPROVEMENT", "1")
    loop = _loop_with_skills(tmp_path)
    from nanobot.cron.service import CronService

    loop.cron_service = CronService(tmp_path / "cron" / "jobs.json")

    status = await cmd_skill_improvement(_ctx(loop, "/skill-improvement schedule status"))
    assert "默认关闭" in status.content

    enabled = await cmd_skill_improvement(
        _ctx(
            loop,
            "/skill-improvement schedule enable every 30 Asia/Shanghai calls=2 tokens=12000 timeout=60",
            is_user_turn=True,
        )
    )
    assert "定时任务已启用" in enabled.content
    assert len(loop.cron_service.list_jobs(include_disabled=True)) == 1
    job = loop.cron_service.list_jobs(include_disabled=True)[0]
    assert job.payload.origin_metadata["devpilot_skill_batch"] is True
    assert job.schedule.every_ms == 30_000

    monkeypatch.setattr(loop, "_cancel_active_tasks", AsyncMock(return_value=1))
    disabled = await cmd_skill_improvement(
        _ctx(loop, "/skill-improvement schedule disable", is_user_turn=True)
    )
    assert "定时任务已停用" in disabled.content
    assert "已取消进行中的任务：1" in disabled.content
    assert loop.cron_service.get_job(job.id).enabled is False

    wall_clock = await cmd_skill_improvement(
        _ctx(
            loop,
            '/skill-improvement schedule enable cron "0 9 * * *" Asia/Shanghai calls=1',
            is_user_turn=True,
        )
    )
    assert 'Cron "0 9 * * *"' in wall_clock.content
    assert loop.cron_service.get_job(job.id).schedule.tz == "Asia/Shanghai"

    manual_runs: list[str] = []

    async def on_job(manual_job):
        manual_runs.append(manual_job.id)
        return "manual-test-result"

    loop.cron_service.on_job = on_job
    manual = await cmd_skill_improvement(
        _ctx(loop, "/skill-improvement schedule run", is_user_turn=True)
    )
    assert "已通过 nanobot 原生 Cron 排队" in manual.content
    assert manual_runs == [job.id]


@pytest.mark.asyncio
async def test_scheduled_candidate_tool_uses_claimed_evidence_and_lease(
    tmp_path: Path, monkeypatch
) -> None:
    data_dir = tmp_path.parent / f"{tmp_path.name}-scheduled-candidate-data"
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

    project_workspace = tmp_path / "project"
    skill_path = project_workspace / "skills" / "bug-fix" / "SKILL.md"
    skill_path.parent.mkdir(parents=True)
    baseline = "---\nname: bug-fix\n---\n\n# Bug fix\n"
    skill_path.write_text(baseline, encoding="utf-8")
    monkeypatch.setenv("DEVPILOT_DATA_DIR", str(data_dir))
    monkeypatch.setenv("DEVPILOT_ENABLE_SKILL_IMPROVEMENT", "1")

    from devpilot.nanobot_tools import SkillCandidateTool
    from devpilot.skill_improvement import (
        SkillImprovementStore,
        content_sha256,
        scheduled_batch_id,
    )

    from nanobot.agent.tools.context import RequestContext, request_context
    from nanobot.command.builtin import _devpilot_project_id

    project_id = _devpilot_project_id(project_workspace)
    store = SkillImprovementStore.from_environment(project_workspace)
    evidence = store.record_feedback(
        task_id="task-scheduled",
        project_id=project_id,
        workspace=project_workspace,
        user_id="user",
        session_id="session-scheduled",
        source_type="user_feedback",
        skill_id="bug-fix",
        skill_hash=content_sha256(baseline),
        correction="先复核失败断言。",
        actual_result="之前的修复没有覆盖失败断言。",
        evidence_refs=["run-scheduled"],
    )
    batch_id = scheduled_batch_id(project_workspace, project_id, "user")
    store.configure_scheduled_batch(
        batch_id=batch_id,
        job_id="cron-scheduled",
        workspace=project_workspace,
        project_id=project_id,
        user_id="user",
        session_key="websocket:chat-scheduled",
        enabled=True,
    )
    claim = store.claim_scheduled_batch("cron-scheduled", lease_owner="test-owner")
    assert claim is not None and claim.status == "claimed"

    context = RequestContext(
        channel="websocket",
        chat_id="chat-scheduled",
        sender_id="user",
        workspace=project_workspace,
        attributes={
            "devpilot_task_id": f"scheduled-{batch_id}",
            "devpilot_project_id": project_id,
            "devpilot_skill_id": "bug-fix",
            "devpilot_batch_id": batch_id,
            "devpilot_batch_owner": "test-owner",
            "devpilot_batch_evidence_ids": [evidence.evidence_id],
        },
    )
    candidate_content = baseline + "\n- Re-check the failing assertion before proposing a fix.\n"
    with request_context(context):
        result = await SkillCandidateTool(project_workspace).execute(
            project_id=project_id,
            skill_path="skills/bug-fix/SKILL.md",
            baseline_content=baseline,
            candidate_content=candidate_content,
            evidence_ids=[evidence.evidence_id],
            reason="The failed task did not re-check the assertion.",
            expected_behavior="The proposal cites the fresh failing assertion.",
            test_set_version="p8-02-v1",
            batch_id=batch_id,
        )

    assert not getattr(result, "is_error", False)
    saved = store.get_scheduled_batch(batch_id)
    assert saved is not None and saved.candidate_id
    completed = store.finish_scheduled_batch(batch_id, lease_owner="test-owner")
    assert completed.status == "candidate_created"


@pytest.mark.asyncio
async def test_unscheduled_candidate_tool_uses_server_owned_bug_fix_target(
    tmp_path: Path, monkeypatch
) -> None:
    data_dir = tmp_path.parent / f"{tmp_path.name}-unscheduled-candidate-data"
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
    monkeypatch.setenv("DEVPILOT_ENABLE_SKILL_IMPROVEMENT", "1")

    from devpilot.nanobot_tools import SkillCandidateTool
    from devpilot.skill_improvement import SkillImprovementStore, content_sha256

    from nanobot.agent.tools.context import RequestContext, request_context

    project_workspace = tmp_path / "project"
    skill_path = project_workspace / "skills" / "bug-fix" / "SKILL.md"
    skill_path.parent.mkdir(parents=True)
    baseline = "---\nname: bug-fix\n---\n\n# Bug fix\n"
    skill_path.write_text(baseline, encoding="utf-8")
    project_id = "project-unscheduled"
    store = SkillImprovementStore.from_environment(project_workspace)
    evidence = store.record_feedback(
        task_id="task-unscheduled",
        project_id=project_id,
        workspace=project_workspace,
        user_id="user",
        session_id="session-unscheduled",
        source_type="user_feedback",
        skill_id="bug-fix",
        skill_hash=content_sha256(baseline),
        correction="先复核失败断言。",
        actual_result="之前的修复没有覆盖失败断言。",
        evidence_refs=["run-unscheduled"],
    )
    context = RequestContext(
        channel="websocket",
        chat_id="chat-unscheduled",
        sender_id="user",
        workspace=project_workspace,
        attributes={
            "devpilot_task_id": "task-unscheduled",
            "devpilot_project_id": project_id,
        },
    )
    with request_context(context):
        result = await SkillCandidateTool(project_workspace).execute(
            project_id=project_id,
            skill_path="skills/repo-analysis/SKILL.md",
            baseline_content=baseline,
            candidate_content=baseline + "\n- Re-check the failing assertion.\n",
            evidence_ids=[evidence.evidence_id],
            reason="The failed task did not re-check the assertion.",
            expected_behavior="The proposal cites the fresh failing assertion.",
            test_set_version="p8-02-v1",
        )

    assert not getattr(result, "is_error", False)
    candidates = store.list_candidates(workspace=project_workspace)
    assert len(candidates) == 1
    assert candidates[0].skill_id == "bug-fix"
    assert candidates[0].skill_path == "skills/bug-fix/SKILL.md"


@pytest.mark.asyncio
async def test_skill_improvement_is_disabled_without_controlled_mode(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.delenv("DEVPILOT_ENABLE_SKILL_IMPROVEMENT", raising=False)
    out = await cmd_skill_improvement(_ctx(_make_loop(tmp_path), "/skill-improvement"))
    assert "未启用" in out.content


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
    monkeypatch.setenv("DEVPILOT_ENABLE_SKILL_IMPROVEMENT", "1")
    loop = _loop_with_skills(tmp_path)
    project_workspace = tmp_path / "project"
    skill_path = project_workspace / "skills" / "repo-analysis" / "SKILL.md"
    skill_path.parent.mkdir(parents=True)
    skill_path.write_text("---\nname: repo-analysis\n---\n\n# Repo analysis\n", encoding="utf-8")
    from nanobot.command.builtin import _devpilot_project_id

    project_id = _devpilot_project_id(project_workspace)
    with sqlite3.connect(data_dir / "devpilot.db") as connection:
        connection.execute(
            "create table projects (id text primary key, real_path text not null)"
        )
        connection.execute(
            "create table runs (id text primary key, project_id text not null, kind text not null, status text not null)"
        )
        connection.execute(
            "create table run_events (id integer primary key autoincrement, run_id text not null, event_type text not null, payload_json text not null)"
        )
        connection.execute(
            "insert into projects values (?, ?)", (project_id, str(project_workspace.resolve()))
        )
        connection.execute(
            "insert into runs values (?, ?, 'repo-analysis', 'completed')",
            ("run-1", project_id),
        )
        connection.execute(
            "insert into run_events (run_id, event_type, payload_json) values (?, ?, ?)",
            (
                "run-1",
                "evidence.search.completed",
                '{"result":"tests/test_api.py", "evidence_refs":["tool-inspect-1"]}',
            ),
        )
        connection.execute(
            "insert into run_events (run_id, event_type, payload_json) values (?, ?, ?)",
            (
                "run-1",
                "run.context.bound",
                '{"task_id":"task-1","session_id":"cli:direct",'
                '"user_id":"user","workspace":"'
                + str(project_workspace.resolve()).replace("\\", "\\\\")
                + '"}',
            ),
        )
    from devpilot.skill_improvement import SkillImprovementStore

    SkillImprovementStore.from_environment(project_workspace).capture_task_snapshot(
        task_id="task-1",
        workspace=project_workspace,
        session_id="cli:direct",
        project_id=project_id,
        skill_id="repo-analysis",
    )
    task = {
        "task_id": "task-1",
        "profile_binding": "repo-analysis@1",
        "run_id": "run-1",
        "evidence_refs": ("run-1", "tool-inspect-1"),
        "file_hashes": ({"path": "tests/test_api.py", "sha256": "a" * 64},),
    }

    rejected = await cmd_skill_improvement(
        _ctx(
            loop,
            "/skill-improvement feedback forged --expected tests/test_api.py",
            session_metadata={"task_context": task},
            workspace=project_workspace,
        )
    )
    assert "用户在聊天中直接发起" in rejected.content

    unrelated = await cmd_skill_improvement(
        _ctx(
            loop,
            "/skill-improvement feedback unrelated fact --expected src/unknown.py",
            is_user_turn=True,
            session_metadata={"task_context": task},
            workspace=project_workspace,
        )
    )
    assert "已记录 repo-analysis 改进案例" not in unrelated.content

    accepted = await cmd_skill_improvement(
        _ctx(
            loop,
        "/skill-improvement feedback the analysis missed the test entry --expected tests/test_api.py",
            is_user_turn=True,
            session_metadata={"task_context": task},
            workspace=project_workspace,
        )
    )
    assert "已记录 repo-analysis 改进案例" in accepted.content

    store = SkillImprovementStore.from_environment(project_workspace)
    cases = store.list_improvement_cases(workspace=project_workspace)
    assert len(cases) == 1
    assert cases[0].skill_id == "repo-analysis"
    assert cases[0].expected_behavior == "tests/test_api.py"


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
