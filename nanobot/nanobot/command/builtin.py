"""Built-in slash command handlers."""

from __future__ import annotations

import asyncio
import difflib
import hashlib
import os
import shlex
import subprocess
import sys
import time
from contextlib import suppress
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, cast

from nanobot import __version__
from nanobot.bus.events import OutboundMessage
from nanobot.command.router import CommandContext, CommandRouter, normalize_command_text
from nanobot.utils.helpers import build_status_content
from nanobot.utils.restart import set_restart_notice_to_env
from nanobot.utils.workspace_prompts import initialize_workspace_prompt

if TYPE_CHECKING:
    from nanobot.agent.loop import AgentLoop
    from nanobot.session.manager import Session
    from nanobot.utils.gitstore import CommitInfo

# WebUI protocol contract for how a slash command participates in turn state:
# - side_channel: returns control text without starting or ending an agent turn.
# - finalize_active_turn: side-channel command that also closes the active UI turn.
# - stop_active_turn: cancels the active turn; WebUI may intercept exact submits.
# - agent_turn: always enters the normal agent path.
# - agent_turn_with_args: no args is side-channel usage; args enter the agent path.
CommandLifecycle = Literal[
    "side_channel",
    "finalize_active_turn",
    "stop_active_turn",
    "agent_turn",
    "agent_turn_with_args",
]


def _devpilot_project_id(workspace: Any) -> str:
    """Derive the stable local project binding used by P8 commands."""
    return "local-" + hashlib.sha256(str(workspace.resolve()).encode()).hexdigest()[:16]


@dataclass(frozen=True)
class BuiltinCommandSpec:
    command: str
    title: str
    description: str
    icon: str
    arg_hint: str = ""
    lifecycle: CommandLifecycle = "side_channel"
    accepts_args: bool = False

    def as_dict(self) -> dict[str, str | bool]:
        return {
            "command": self.command,
            "title": self.title,
            "description": self.description,
            "icon": self.icon,
            "arg_hint": self.arg_hint,
            "lifecycle": self.lifecycle,
            "accepts_args": self.accepts_args,
        }


BUILTIN_COMMAND_SPECS: tuple[BuiltinCommandSpec, ...] = (
    BuiltinCommandSpec(
        "/new",
        "New chat",
        "Reset this chat and start a fresh conversation.",
        "square-pen",
        lifecycle="finalize_active_turn",
    ),
    BuiltinCommandSpec(
        "/stop",
        "Stop current task",
        "Cancel the active agent turn for this chat.",
        "square",
        lifecycle="stop_active_turn",
    ),
    BuiltinCommandSpec(
        "/restart",
        "Restart nanobot",
        "Restart the bot process.",
        "rotate-cw",
    ),
    BuiltinCommandSpec(
        "/status",
        "Show status",
        "Display runtime, provider, and channel status.",
        "activity",
    ),
    BuiltinCommandSpec(
        "/model",
        "Switch model preset",
        "Show or switch the active model preset.",
        "brain",
        "[preset]",
        accepts_args=True,
    ),
    BuiltinCommandSpec(
        "/task-profile",
        "Select task profile",
        "Choose a trusted Skill and tool set for this chat session.",
        "sliders-horizontal",
        "[name|off]",
        accepts_args=True,
    ),
    BuiltinCommandSpec(
        "/history",
        "Show conversation history",
        "Print the last N persisted conversation messages.",
        "history",
        "[n]",
        accepts_args=True,
    ),
    BuiltinCommandSpec(
        "/goal",
        "Start long-running goal",
        "Tell the agent to treat the request as a long-running goal.",
        "activity",
        "<goal>",
        lifecycle="agent_turn_with_args",
        accepts_args=True,
    ),
    BuiltinCommandSpec(
        "/trigger",
        "Create named local trigger",
        "Create a named CLI trigger bound to this chat session.",
        "zap",
        "<name>",
        accepts_args=True,
    ),
    BuiltinCommandSpec(
        "/dream",
        "Run Dream",
        "Manually trigger memory consolidation.",
        "sparkles",
    ),
    BuiltinCommandSpec(
        "/dream-log",
        "Show Dream log",
        "Show what the last Dream consolidation changed.",
        "book-open",
        accepts_args=True,
    ),
    BuiltinCommandSpec(
        "/dream-restore",
        "Restore memory",
        "Revert memory to a previous Dream snapshot.",
        "undo-2",
        accepts_args=True,
    ),
    BuiltinCommandSpec(
        "/dream-prompt",
        "Dream memory",
        "Tell Dream how to organize this workspace's memory.",
        "file-text",
        "[init]",
        accepts_args=True,
    ),
    BuiltinCommandSpec(
        "/evaluator-prompt",
        "Heartbeat evaluator",
        "Customize the heartbeat notification gate prompt for this workspace.",
        "file-text",
        "[init]",
        accepts_args=True,
    ),
    BuiltinCommandSpec(
        "/skill",
        "List skills",
        "List all enabled skills available to the agent.",
        "wrench",
    ),
    BuiltinCommandSpec(
        "/skill-improvement",
        "审查 Skill 改进",
        "查看 Skill 候选，或在聊天中配置可选的原生 Cron 批处理。",
        "wrench",
        "[schedule status|run|disable|enable ...|candidate-id ...|delete-evidence evidence-id]",
        accepts_args=True,
    ),
    BuiltinCommandSpec(
        "/help",
        "Show help",
        "List available slash commands.",
        "circle-help",
    ),
    BuiltinCommandSpec(
        "/pairing",
        "Manage pairing",
        "List, approve, deny or revoke pairing requests.",
        "shield",
        "[list|approve <code>|deny <code>|revoke <user_id>]",
        accepts_args=True,
    ),
)


def builtin_command_palette() -> list[dict[str, str | bool]]:
    """Return structured command metadata for UI command palettes."""
    return [spec.as_dict() for spec in BUILTIN_COMMAND_SPECS]


def builtin_command_starts_agent_turn(text: str) -> bool:
    """Return whether WebUI ingress should expect a normal agent lifecycle."""
    normalized = normalize_command_text(text)
    command, separator, args = normalized.partition(" ")
    spec = next(
        (item for item in BUILTIN_COMMAND_SPECS if item.command == command.lower()),
        None,
    )
    if spec is None or (separator and not spec.accepts_args):
        return True
    if spec.lifecycle == "agent_turn":
        return True
    return spec.lifecycle == "agent_turn_with_args" and bool(args.strip())


async def cmd_stop(ctx: CommandContext) -> OutboundMessage:
    """Cancel all active tasks and subagents for the session."""
    loop = ctx.loop
    msg = ctx.msg
    total = await loop._cancel_active_tasks(ctx.key)  # pyright: ignore[reportPrivateUsage]
    # Also drain pending queue to prevent mid-turn injection deadlock
    pending = loop._pending_queues.pop(ctx.key, None)  # pyright: ignore[reportPrivateUsage]
    if pending is not None:
        while not pending.empty():
            try:
                pending.get_nowait()
                total += 1
            except Exception:
                break
    content = f"Stopped {total} task(s)." if total else "No active task to stop."
    return OutboundMessage(
        channel=msg.channel, chat_id=msg.chat_id, content=content,
        metadata=dict(msg.metadata or {})
    )


async def cmd_restart(ctx: CommandContext) -> OutboundMessage:
    """Restart the process."""
    msg = ctx.msg
    set_restart_notice_to_env(
        channel=msg.channel,
        chat_id=msg.chat_id,
        metadata=dict(msg.metadata or {}),
    )

    async def _do_restart():
        await asyncio.sleep(1)
        argv = [sys.executable, "-m", "nanobot"] + sys.argv[1:]
        mode = ctx.loop.restart_mode or "auto"
        if mode == "auto":
            mode = "spawn" if sys.platform == "win32" else "exec"
        if mode == "exec":
            os.execv(sys.executable, argv)
            return
        if mode == "spawn":
            kwargs: dict[str, Any] = {}
            if sys.platform == "win32":
                kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
            subprocess.Popen(argv, **kwargs)
        os._exit(0)

    asyncio.create_task(_do_restart())
    return OutboundMessage(
        channel=msg.channel, chat_id=msg.chat_id, content="Restarting...",
        metadata=dict(msg.metadata or {})
    )


async def cmd_status(ctx: CommandContext) -> OutboundMessage:
    """Build an outbound status message for a session."""
    loop = ctx.loop
    session = ctx.session or loop.sessions.get_or_create(ctx.key)
    runtime = ctx.runtime or loop.runtime_for_session(session)
    ctx_est = 0
    with suppress(Exception):
        ctx_est, _ = loop.consolidator.estimate_session_prompt_tokens(
            session,
            runtime=runtime,
        )
    if ctx_est <= 0:
        ctx_est = loop._last_usage.get("prompt_tokens", 0)  # pyright: ignore[reportPrivateUsage]

    # Fetch web search provider usage (best-effort, never blocks the response)
    search_usage_text: str | None = None
    # Never let usage fetch break /status
    with suppress(Exception):
        from nanobot.utils.searchusage import fetch_search_usage
        search_cfg = loop.web_config.search
        usage = await fetch_search_usage(
            provider=search_cfg.provider,
            api_key=search_cfg.api_key or None,
        )
        search_usage_text = usage.format()
    active_tasks = loop._active_tasks.get(ctx.key, [])  # pyright: ignore[reportPrivateUsage]
    task_count = sum(1 for t in active_tasks if not t.done())
    with suppress(Exception):
        task_count += loop.subagents.get_running_count_by_session(ctx.key)
    return OutboundMessage(
        channel=ctx.msg.channel,
        chat_id=ctx.msg.chat_id,
        content=build_status_content(
            version=__version__, model=runtime.model,
            start_time=loop._start_time, last_usage=loop._last_usage,  # pyright: ignore[reportPrivateUsage]
            context_window_tokens=runtime.context_window_tokens,
            session_msg_count=len(session.get_history(max_messages=0)),
            context_tokens_estimate=ctx_est,
            search_usage_text=search_usage_text,
            active_task_count=task_count,
            max_completion_tokens=runtime.generation.max_tokens,
        ),
        metadata={**dict(ctx.msg.metadata or {}), "render_as": "text"},
    )


async def cmd_new(ctx: CommandContext) -> OutboundMessage:
    """Stop active task and start a fresh session."""
    loop = ctx.loop
    await loop._cancel_active_tasks(ctx.key)  # pyright: ignore[reportPrivateUsage]
    session = ctx.session or loop.sessions.get_or_create(ctx.key)
    snapshot = session.messages[session.last_consolidated:]
    runtime = None
    if snapshot:
        runtime = ctx.runtime or loop.runtime_for_session(session)
    session.clear()
    # A fresh chat must receive a fresh Skill snapshot; the selected trusted
    # profile itself remains active for the next task.
    session.metadata.pop("task_context", None)
    session.metadata.pop("task_loop_guard", None)
    loop.sessions.save(session)
    loop.sessions.invalidate(session.key)
    if snapshot and runtime is not None:
        loop.schedule_background(
            loop.consolidator.archive(  # pyright: ignore[reportUnknownMemberType]
                snapshot,
                runtime=runtime,
                session_key=ctx.key,
            )
        )
    return OutboundMessage(
        channel=ctx.msg.channel, chat_id=ctx.msg.chat_id,
        content="New session started.",
        metadata=dict(ctx.msg.metadata or {})
    )


def _format_preset_names(names: list[str]) -> str:
    return ", ".join(f"`{name}`" for name in names) if names else "(none configured)"


def _model_preset_names(loop: AgentLoop) -> list[str]:
    names = set(loop.model_presets)
    names.add("default")
    return ["default", *sorted(name for name in names if name != "default")]


def _command_error_message(exc: Exception) -> str:
    return str(exc.args[0]) if isinstance(exc, KeyError) and exc.args else str(exc)


def _model_command_status(loop: AgentLoop, session: Session) -> str:
    names = _model_preset_names(loop)
    try:
        runtime = loop.runtime_for_session(session, recover_removed=False)
    except (KeyError, ValueError) as exc:
        return "\n".join([
            "## Model",
            f"- Current selection error: {_command_error_message(exc)}",
            f"- Available presets: {_format_preset_names(names)}",
            "- Switch with `/model <preset>`.",
        ])
    active = runtime.model_preset or "default"
    return "\n".join([
        "## Model",
        f"- Current model: `{runtime.model}`",
        f"- Current preset: `{active}`",
        f"- Available presets: {_format_preset_names(names)}",
    ])


async def cmd_model(ctx: CommandContext) -> OutboundMessage:
    """Show or switch model presets."""
    loop = ctx.loop
    args = ctx.args.strip()
    metadata = {**dict(ctx.msg.metadata or {}), "render_as": "text"}

    if not args:
        session = ctx.session or loop.sessions.get_or_create(ctx.key)
        return OutboundMessage(
            channel=ctx.msg.channel,
            chat_id=ctx.msg.chat_id,
            content=_model_command_status(loop, session),
            metadata=metadata,
        )

    parts = args.split()
    if len(parts) != 1:
        return OutboundMessage(
            channel=ctx.msg.channel,
            chat_id=ctx.msg.chat_id,
            content="Usage: `/model [preset]`",
            metadata=metadata,
        )

    name = parts[0]
    try:
        runtime = loop.set_session_model_preset(ctx.key, name)
    except (KeyError, ValueError) as exc:
        names = _model_preset_names(loop)
        return OutboundMessage(
            channel=ctx.msg.channel,
            chat_id=ctx.msg.chat_id,
            content=(
                f"Could not switch model preset: {_command_error_message(exc)}\n\n"
                f"Available presets: {_format_preset_names(names)}"
            ),
            metadata=metadata,
        )

    max_tokens = runtime.generation.max_tokens
    lines = [
        f"Switched model preset to `{runtime.model_preset}`.",
        "- Scope: current session",
        f"- Model: `{runtime.model}`",
        f"- Context window: {runtime.context_window_tokens}",
    ]
    lines.append(f"- Max output tokens: {max_tokens}")
    return OutboundMessage(
        channel=ctx.msg.channel,
        chat_id=ctx.msg.chat_id,
        content="\n".join(lines),
        metadata=metadata,
    )


async def cmd_task_profile(ctx: CommandContext) -> OutboundMessage:
    """Show or select a developer-owned task capability profile."""
    loop = ctx.loop
    metadata = {**dict(ctx.msg.metadata or {}), "render_as": "text"}
    session = ctx.session or loop.sessions.get_or_create(ctx.key)
    args = ctx.args.strip()
    if not args:
        return OutboundMessage(
            channel=ctx.msg.channel,
            chat_id=ctx.msg.chat_id,
            content=loop.describe_task_profiles(session),
            metadata=metadata,
        )
    if not ctx.is_user_turn:
        return OutboundMessage(
            channel=ctx.msg.channel,
            chat_id=ctx.msg.chat_id,
            content="Only a direct user message can change the task profile; this message may only query `/task-profile`.",
            metadata=metadata,
        )
    parts = args.split()
    if len(parts) != 1:
        return OutboundMessage(
            channel=ctx.msg.channel,
            chat_id=ctx.msg.chat_id,
            content="Usage: `/task-profile [repo-analysis|feature-delivery|bug-fix|off]`",
            metadata=metadata,
        )
    name = parts[0].lower()
    if name in {"off", "none", "default"}:
        try:
            loop.clear_session_task_profile(ctx.key)
        except ValueError as exc:
            return OutboundMessage(
                channel=ctx.msg.channel,
                chat_id=ctx.msg.chat_id,
                content=f"Could not clear task profile: {exc}",
                metadata=metadata,
            )
        return OutboundMessage(
            channel=ctx.msg.channel,
            chat_id=ctx.msg.chat_id,
            content="Task profile cleared; this chat uses nanobot's ordinary configured tools.",
            metadata=metadata,
        )
    try:
        profile = loop.set_session_task_profile(ctx.key, name)
    except (KeyError, ValueError) as exc:
        return OutboundMessage(
            channel=ctx.msg.channel,
            chat_id=ctx.msg.chat_id,
            content=f"Could not select task profile: {exc}",
            metadata=metadata,
        )
    return OutboundMessage(
        channel=ctx.msg.channel,
        chat_id=ctx.msg.chat_id,
        content=(
            f"Task profile `{profile.name}` is active for this chat.\n"
            f"Skills: {', '.join(profile.skill_names)}\n"
            f"Visible tools: {', '.join(sorted(profile.allowed_tools))}\n"
            f"Output fields: {', '.join(profile.output_contract)}\n"
            "This selection does not grant approval, execution, or rollback authority."
        ),
        metadata=metadata,
    )


async def cmd_dream(ctx: CommandContext) -> OutboundMessage:
    """Manually trigger a Dream consolidation run."""
    import time

    loop = ctx.loop
    msg = ctx.msg

    async def _run_dream():
        from nanobot.agent.memory import DreamRunProgress, MemoryStore

        dream_session_key = MemoryStore.dream_session_key
        build_dream_commit_message = MemoryStore.build_dream_commit_message
        prune_dream_sessions = MemoryStore.prune_dream_sessions

        store = loop.context.memory
        progress = DreamRunProgress()
        content = ""
        resp = None
        diff_body = ""
        t0 = time.monotonic()
        try:
            result = store.build_dream_prompt()
            if result is None:
                await loop.bus.publish_outbound(OutboundMessage(
                    channel=msg.channel, chat_id=msg.chat_id,
                    content=_format_dream_no_input_message(),
                    metadata={"render_as": "text"},
                ))
                return
            prompt, last_cursor = result
            key = dream_session_key()
            dream_runtime = loop.dream_runtime()
            resp = await loop.process_direct(
                prompt,
                session_key=key,
                ephemeral=True,
                tools=store.build_dream_tools(),
                on_progress=progress,
                runtime=dream_runtime,
            )
            elapsed = time.monotonic() - t0
            # The real file delta grounds the audit record; clean completion
            # decides whether this history batch has finished processing.
            diff_body = store.dream_content_diff()
            completed = MemoryStore.dream_run_completed(
                resp,
                had_tool_errors=progress.had_tool_errors,
            )
            if completed:
                store.set_last_dream_cursor(last_cursor)
                if diff_body:
                    content = f"Dream completed in {elapsed:.1f}s."
                else:
                    content = f"Dream completed in {elapsed:.1f}s; no memory changes."
            else:
                content = (
                    f"Dream did not complete after {elapsed:.1f}s; "
                    "memory cursor was not advanced."
                )
        except Exception as e:
            elapsed = time.monotonic() - t0
            content = f"Dream failed after {elapsed:.1f}s: {e}"
        finally:
            from nanobot.webui.token_usage import record_response_token_usage

            record_response_token_usage(
                resp,
                source="dream",
                timezone_name=getattr(loop.context, "timezone", None),
            )
            if store.git.is_initialized():
                commit_msg = build_dream_commit_message("dream: manual run", diff_body)
                sha = store.git.auto_commit(commit_msg)
                if sha:
                    content += f" (commit {sha})"
            store.compact_history()
            prune_dream_sessions(loop.sessions.sessions_dir)
        await loop.bus.publish_outbound(OutboundMessage(
            channel=msg.channel, chat_id=msg.chat_id, content=content,
        ))

    asyncio.create_task(_run_dream())
    return OutboundMessage(
        channel=msg.channel, chat_id=msg.chat_id, content="Dreaming...",
    )


async def cmd_dream_prompt(ctx: CommandContext) -> OutboundMessage:
    """Show or set up the workspace Dream memory instructions."""
    store = ctx.loop.context.memory
    path = store.dream_prompt_file
    display_path = path.relative_to(store.workspace).as_posix()
    args = ctx.args.strip().lower()

    if args == "init":
        if not initialize_workspace_prompt(path, store.default_dream_prompt()):
            content = (
                f"Dream memory instructions already exist at `{display_path}`.\n\n"
                "Edit that file, or delete/empty it to return to nanobot's default."
            )
        else:
            content = (
                f"Created Dream memory instructions at `{display_path}`.\n\n"
                "Edit that file to teach Dream how to organize memory. "
                "This fully replaces nanobot's default Dream guide for this workspace. "
                "Delete or empty it to return to nanobot's default."
            )
    elif args:
        content = "Usage: /dream-prompt [init]"
    elif store.has_dream_prompt_override():
        content = (
            "Dream memory instructions: custom for this workspace\n\n"
            f"- Path: `{display_path}`\n"
            "- Delete or empty this file to return to nanobot's default."
        )
    else:
        content = (
            "Dream memory instructions: nanobot default\n\n"
            f"- Editable file: `{display_path}`\n"
            "- Run `/dream-prompt init` to create an editable copy."
        )

    return OutboundMessage(
        channel=ctx.msg.channel,
        chat_id=ctx.msg.chat_id,
        content=content,
        metadata={**dict(ctx.msg.metadata or {}), "render_as": "text"},
    )


async def cmd_evaluator_prompt(ctx: CommandContext) -> OutboundMessage:
    """Show or set up the workspace heartbeat evaluator prompt."""
    from nanobot.utils.evaluator import (
        default_evaluator_prompt,
        evaluator_prompt_file,
        has_evaluator_prompt_override,
    )

    workspace = ctx.loop.context.memory.workspace
    path = evaluator_prompt_file(workspace)
    display_path = path.relative_to(workspace).as_posix()
    args = ctx.args.strip().lower()

    if args == "init":
        if not initialize_workspace_prompt(path, default_evaluator_prompt()):
            content = (
                f"Heartbeat evaluator prompt already exists at `{display_path}`.\n\n"
                "Edit that file, or delete/empty it to return to nanobot's default."
            )
        else:
            content = (
                f"Created heartbeat evaluator prompt at `{display_path}`.\n\n"
                "Edit that file to control when the heartbeat notification gate speaks. "
                "It must still instruct the model to call the `evaluate_notification` tool, "
                "otherwise the gate fails closed and stays silent. "
                "Delete or empty it to return to nanobot's default."
            )
    elif args:
        content = "Usage: /evaluator-prompt [init]"
    elif has_evaluator_prompt_override(workspace):
        content = (
            "Heartbeat evaluator prompt: custom for this workspace\n\n"
            f"- Path: `{display_path}`\n"
            "- Delete or empty this file to return to nanobot's default."
        )
    else:
        content = (
            "Heartbeat evaluator prompt: nanobot default\n\n"
            f"- Editable file: `{display_path}`\n"
            "- Run `/evaluator-prompt init` to create an editable copy."
        )

    return OutboundMessage(
        channel=ctx.msg.channel,
        chat_id=ctx.msg.chat_id,
        content=content,
        metadata={**dict(ctx.msg.metadata or {}), "render_as": "text"},
    )


def _format_dream_no_input_message() -> str:
    return "\n".join([
        "Dream has no conversation history to process yet.",
        "",
        "Dream reads new entries from `memory/history.jsonl` after the current Dream cursor.",
        (
            "Short chats only reach that file after token compaction or idle auto-compact, "
            "so a fresh or short WebUI chat may leave Dream with no input."
        ),
        "",
        "Next steps:",
        "- Enable `agents.defaults.idleCompactAfterMinutes` so completed chats become Dream input automatically.",
        "- Compact the current chat into memory once that manual action is available.",
        "- If you expected history to exist, check whether `memory/history.jsonl` has new entries after the Dream cursor.",
        "- Use `/dream-prompt` to see or change how Dream organizes memory.",
    ])


def _extract_changed_files(diff: str) -> list[str]:
    """Extract changed file paths from a unified diff."""
    files: list[str] = []
    seen: set[str] = set()
    for line in diff.splitlines():
        if not line.startswith("diff --git "):
            continue
        parts = line.split()
        if len(parts) < 4:
            continue
        path = parts[3]
        if path.startswith("b/"):
            path = path[2:]
        if path in seen:
            continue
        seen.add(path)
        files.append(path)
    return files


def _format_changed_files(diff: str) -> str:
    files = _extract_changed_files(diff)
    if not files:
        return "No tracked memory files changed."
    return ", ".join(f"`{path}`" for path in files)


_DREAM_COMMIT_PREFIX = "dream:"


def _format_dream_log_content(
    commit: CommitInfo,
    diff: str,
    *,
    requested_sha: str | None = None,
) -> str:
    files_line = _format_changed_files(diff)
    lines = [
        "## Dream Update",
        "",
        "Here is the selected Dream memory change." if requested_sha else "Here is the latest Dream memory change.",
        "",
        f"- Commit: `{commit.sha}`",
        f"- Time: {commit.timestamp}",
        f"- Changed files: {files_line}",
    ]
    if diff:
        lines.extend([
            "",
            f"Use `/dream-restore {commit.sha}` to undo this change.",
            "",
            "```diff",
            diff.rstrip(),
            "```",
        ])
    else:
        lines.extend([
            "",
            "Dream recorded this version, but there is no file diff to display.",
        ])
    return "\n".join(lines)


def _format_dream_restore_list(commits: list[CommitInfo]) -> str:
    lines = [
        "## Dream Restore",
        "",
        "Choose a Dream memory version to restore. Latest first:",
        "",
    ]
    for c in commits:
        lines.append(f"- `{c.sha}` {c.timestamp} - {c.subject()}")
    lines.extend([
        "",
        "Preview a version with `/dream-log <sha>` before restoring it.",
        "Restore a version with `/dream-restore <sha>`.",
    ])
    return "\n".join(lines)


async def cmd_dream_log(ctx: CommandContext) -> OutboundMessage:
    """Show what the last Dream changed.

    Default: diff of the latest Dream commit versus its parent.
    With /dream-log <sha>: diff of that specific commit.
    """
    store = ctx.loop.consolidator.store
    git = store.git

    if not git.is_initialized():
        if store.get_last_dream_cursor() == 0:
            msg = (
                "Dream has not run yet. Run `/dream`, or wait for the next scheduled Dream cycle.\n\n"
                "Use `/dream-prompt` to see or change how Dream organizes memory."
            )
        else:
            msg = "Dream history is not available because memory versioning is not initialized."
        return OutboundMessage(
            channel=ctx.msg.channel, chat_id=ctx.msg.chat_id,
            content=msg, metadata={"render_as": "text"},
        )

    args = ctx.args.strip()

    if args:
        # Show diff of a specific commit
        sha = args.split()[0]
        result = git.show_commit_diff(sha)
        if not result:
            content = (
                f"Couldn't find Dream change `{sha}`.\n\n"
                "Use `/dream-restore` to list recent versions, "
                "or `/dream-log` to inspect the latest one."
            )
        else:
            commit, diff = result
            content = _format_dream_log_content(commit, diff, requested_sha=sha)
    else:
        # Default: show the latest Dream commit's diff
        commits = git.log(max_entries=1, message_prefix=_DREAM_COMMIT_PREFIX)
        result = (
            git.show_commit_diff(
                commits[0].sha,
                max_entries=1,
                message_prefix=_DREAM_COMMIT_PREFIX,
            )
            if commits else None
        )
        if result:
            commit, diff = result
            content = _format_dream_log_content(commit, diff)
        else:
            content = (
                "Dream memory has no saved versions yet.\n\n"
                "Use `/dream-prompt` to see or change how Dream organizes memory."
            )

    return OutboundMessage(
        channel=ctx.msg.channel, chat_id=ctx.msg.chat_id,
        content=content, metadata={"render_as": "text"},
    )


async def cmd_dream_restore(ctx: CommandContext) -> OutboundMessage:
    """Restore memory files from a previous dream commit.

    Usage:
        /dream-restore          — list recent commits
        /dream-restore <sha>    — revert a specific commit
    """
    store = ctx.loop.consolidator.store
    git = store.git
    if not git.is_initialized():
        return OutboundMessage(
            channel=ctx.msg.channel, chat_id=ctx.msg.chat_id,
            content="Dream history is not available because memory versioning is not initialized.",
        )

    args = ctx.args.strip()
    if not args:
        # Show recent Dream commits for the user to pick
        commits = git.log(max_entries=10, message_prefix=_DREAM_COMMIT_PREFIX)
        if not commits:
            content = "Dream memory has no saved versions to restore yet."
        else:
            content = _format_dream_restore_list(commits)
    else:
        sha = args.split()[0]
        result = git.show_commit_diff(sha, message_prefix=_DREAM_COMMIT_PREFIX)
        if not result:
            content = (
                f"Couldn't restore Dream change `{sha}`.\n\n"
                "Only Dream memory versions can be restored. "
                "Use `/dream-restore` to list recent versions."
            )
        else:
            changed_files = _format_changed_files(result[1])
            new_sha = git.revert(sha, message_prefix=_DREAM_COMMIT_PREFIX)
            if new_sha:
                content = (
                    f"Restored Dream memory to the state before `{sha}`.\n\n"
                    f"- New safety commit: `{new_sha}`\n"
                    f"- Restored files: {changed_files}\n\n"
                    f"Use `/dream-log {new_sha}` to inspect the restore diff."
                )
            else:
                content = (
                    f"Couldn't restore Dream change `{sha}`.\n\n"
                    "It may be the first saved version with no earlier state to restore."
                )
    return OutboundMessage(
        channel=ctx.msg.channel, chat_id=ctx.msg.chat_id,
        content=content, metadata={"render_as": "text"},
    )


_HISTORY_DEFAULT_COUNT = 10
_HISTORY_MAX_COUNT = 50
_HISTORY_MAX_CONTENT_CHARS = 200


def _format_history_message(msg: dict[str, Any]) -> str | None:
    """Format a single history message for display. Returns None to skip."""
    role = msg.get("role")
    if role not in ("user", "assistant"):
        return None
    content = msg.get("content") or ""
    if isinstance(content, list):
        parts = [
            text
            for block in cast(list[object], content)
            if (item := cast(dict[str, Any], block) if isinstance(block, dict) else None)
            and item.get("type") == "text"
            and isinstance(text := item.get("text"), str)
        ]
        content = " ".join(parts)
    content = str(content).strip()
    if not content:
        return None
    if len(content) > _HISTORY_MAX_CONTENT_CHARS:
        content = content[:_HISTORY_MAX_CONTENT_CHARS] + "…"
    label = "👤 You" if role == "user" else "🤖 Bot"
    return f"{label}: {content}"


async def cmd_history(ctx: CommandContext) -> OutboundMessage:
    """Show the last N messages of the current session (default 10, max 50).

    Usage: /history [count]
    """
    count = _HISTORY_DEFAULT_COUNT
    if ctx.args.strip():
        try:
            count = max(1, min(int(ctx.args.strip()), _HISTORY_MAX_COUNT))
        except ValueError:
            return OutboundMessage(
                channel=ctx.msg.channel, chat_id=ctx.msg.chat_id,
                content="Usage: /history [count] — e.g. /history 5 (default: 10, max: 50)",
                metadata=dict(ctx.msg.metadata or {}),
            )

    session = ctx.session or ctx.loop.sessions.get_or_create(ctx.key)
    history = session.get_history(max_messages=0, include_runtime_context=False)
    visible = [_format_history_message(m) for m in history]
    visible = [m for m in visible if m is not None]
    recent = visible[-count:]

    if not recent:
        return OutboundMessage(
            channel=ctx.msg.channel, chat_id=ctx.msg.chat_id,
            content="No conversation history yet.",
            metadata=dict(ctx.msg.metadata or {}),
        )

    header = f"Last {len(recent)} message(s):\n"
    return OutboundMessage(
        channel=ctx.msg.channel, chat_id=ctx.msg.chat_id,
        content=header + "\n".join(recent),
        metadata={**dict(ctx.msg.metadata or {}), "render_as": "text"},
    )


async def cmd_goal(ctx: CommandContext) -> OutboundMessage | None:
    """Mark this turn as an explicit sustained-goal request."""
    from nanobot.agent.goal_permission import goal_mutation_permission

    goal = ctx.args.strip()
    if not goal:
        return OutboundMessage(
            channel=ctx.msg.channel,
            chat_id=ctx.msg.chat_id,
            content="Usage: /goal <long-running task description>",
            metadata={**dict(ctx.msg.metadata or {}), "render_as": "text"},
        )
    if ctx.session is None:
        return OutboundMessage(
            channel=ctx.msg.channel,
            chat_id=ctx.msg.chat_id,
            content=(
                "A task is already running for this chat. "
                "Use `/stop` first, then send `/goal <long-running task description>` again."
            ),
            metadata={**dict(ctx.msg.metadata or {}), "render_as": "text"},
        )
    if not ctx.is_user_turn:
        return OutboundMessage(
            channel=ctx.msg.channel,
            chat_id=ctx.msg.chat_id,
            content="Goal mode can only be started by a user `/goal <task>` command.",
            metadata={**dict(ctx.msg.metadata or {}), "render_as": "text"},
        )

    ctx.turn_scopes.append(goal_mutation_permission(True))
    ctx.msg.metadata = {
        **dict(ctx.msg.metadata or {}),
        "original_command": "/goal",
        "original_content": ctx.raw,
        "goal_requested": True,
        "goal_started_at": time.time(),
    }
    ctx.msg.content = ctx.raw
    return None


async def cmd_pairing(ctx: CommandContext) -> OutboundMessage:
    """List, approve, deny or revoke pairing requests."""
    from nanobot.pairing import PAIRING_COMMAND_META_KEY, handle_pairing_command

    reply = handle_pairing_command(ctx.msg.channel, ctx.args)
    return OutboundMessage(
        channel=ctx.msg.channel,
        chat_id=ctx.msg.chat_id,
        content=reply,
        metadata={PAIRING_COMMAND_META_KEY: True},
    )


async def cmd_skill(ctx: CommandContext) -> OutboundMessage:
    """List all enabled skills (name and description only)."""
    loop = ctx.loop
    skills = loop.context.skills.list_skills(filter_unavailable=False)
    if not skills:
        content = "No skills available."
    else:
        lines = [f"Available skills ({len(skills)}):", ""]
        for entry in skills:
            desc = loop.context.skills.get_skill_description(entry["name"])
            lines.append(f"- **{entry['name']}** — {desc}")
        content = "\n".join(lines)
    return OutboundMessage(
        channel=ctx.msg.channel,
        chat_id=ctx.msg.chat_id,
        content=content,
        metadata=dict(ctx.msg.metadata or {}),
    )


def _parse_skill_batch_schedule(
    values: list[str],
    *,
    default_timezone: str = "Asia/Shanghai",
) -> tuple[Any, dict[str, int], str] | str:
    """Parse the small, explicit P8-03 schedule grammar."""
    from nanobot.cron.types import CronSchedule

    if len(values) < 2 or values[0].lower() not in {"every", "cron"}:
        return (
            "用法：/skill-improvement schedule enable every <秒数> [时区]，"
            "或 cron \"<5 位表达式>\" [时区] "
            "[calls=1|2] [tokens=512-20000] [timeout=30-600]"
        )
    schedule_kind = values[0].lower()
    timezone = default_timezone
    option_start = 2
    if schedule_kind == "every":
        try:
            seconds = int(values[1])
        except ValueError:
            return "定时间隔必须是整数秒。"
        if seconds < 30:
            return "定时间隔至少为 30 秒。"
        if len(values) > 2 and "=" not in values[2]:
            timezone = values[2]
            option_start = 3
    else:
        timezone = values[2] if len(values) > 2 and "=" not in values[2] else default_timezone
        option_start = 3 if len(values) > 2 and "=" not in values[2] else 2
    options = {"calls": 2, "tokens": 12_000, "timeout": 600}
    for value in values[option_start:]:
        if "=" not in value:
            return f"未知的定时选项 `{value}`。"
        key, raw = value.split("=", 1)
        if key not in options:
            return f"未知的定时选项 `{key}`。"
        try:
            options[key] = int(raw)
        except ValueError:
            return f"定时选项 `{key}` 必须是整数。"
    if options["calls"] not in {1, 2}:
        return "每批调用次数必须是 1 或 2。"
    if not 512 <= options["tokens"] <= 20_000:
        return "每批 token 上限必须在 512 到 20000 之间。"
    if not 30 <= options["timeout"] <= 600:
        return "每批超时时间必须在 30 到 600 秒之间。"
    try:
        if schedule_kind == "every":
            # ``every`` is elapsed-time based in nanobot, so CronSchedule does
            # not attach a timezone to it.  Use ``cron`` for wall-clock times.
            schedule = CronSchedule(kind="every", every_ms=seconds * 1000)
        else:
            schedule = CronSchedule(kind="cron", expr=values[1], tz=timezone)
    except (TypeError, ValueError) as exc:
        return f"定时配置无效：{_skill_exception_label(exc)}。"
    return schedule, options, timezone


_SKILL_STATUS_LABELS = {
    "idle": "空闲",
    "disabled": "已停用",
    "running": "运行中",
    "no_new_evidence": "没有新证据",
    "failed": "失败",
    "candidate_created": "已生成候选",
    "budget_exhausted": "预算已用尽",
    "pending_evaluation": "等待评测",
    "evaluation_failed": "评测失败",
    "pending_confirmation": "等待确认",
    "active": "已启用",
    "enabled": "已启用",
    "ignored": "已忽略",
    "stale": "已过期",
    "eligible": "符合启用条件",
    "no_verified_improvement": "未发现已验证改进",
}


def _skill_status_label(status: str) -> str:
    """Translate internal Skill states for native-chat output."""
    return _SKILL_STATUS_LABELS.get(status, f"未识别状态（{status}）")


_SKILL_EXCEPTION_LABELS = {
    "ImportError": "依赖未加载",
    "OSError": "本地存储或文件操作失败",
    "TypeError": "参数类型错误",
    "ValueError": "参数或状态不合法",
}


def _skill_exception_label(exc: BaseException) -> str:
    """Translate implementation errors before returning them to native chat."""
    return _SKILL_EXCEPTION_LABELS.get(type(exc).__name__, "内部处理失败")


async def cmd_skill_improvement(ctx: CommandContext) -> OutboundMessage:
    """Expose the P8 candidate lifecycle through nanobot's native chat."""
    if os.environ.get("DEVPILOT_ENABLE_SKILL_IMPROVEMENT") != "1":
        return OutboundMessage(
            channel=ctx.msg.channel,
            chat_id=ctx.msg.chat_id,
            content=(
                "Skill 改进功能当前未启用；如需受控试用，请在 nanobot 启动进程中设置 "
                "DEVPILOT_ENABLE_SKILL_IMPROVEMENT=1。"
            ),
        )
    try:
        from devpilot.skill_improvement import SkillImprovementStore, scheduled_batch_id

        workspace = ctx.workspace or ctx.loop.context.memory.workspace
        store = SkillImprovementStore.from_environment(workspace)
    except (ImportError, ValueError, OSError) as exc:
        content = f"Skill 改进功能不可用：{_skill_exception_label(exc)}。"
        return OutboundMessage(channel=ctx.msg.channel, chat_id=ctx.msg.chat_id, content=content)

    args = ctx.args.strip().split()
    user_id = getattr(ctx.msg, "sender_id", None) or ctx.msg.chat_id
    project_id = _devpilot_project_id(workspace)
    if not args:
        candidates = store.list_candidates(
            workspace=workspace, user_id=user_id, project_id=project_id
        )
        if not candidates:
            content = "暂无私有 Skill 候选。"
        else:
            lines = ["私有 Skill 候选：", ""]
            lines.extend(
                f"- `{item.candidate_id}` — {_skill_status_label(item.status)}"
                for item in candidates
            )
            lines.append("\n使用 `/skill-improvement <候选 ID>` 查看候选详情。")
            content = "\n".join(lines)
        return OutboundMessage(
            channel=ctx.msg.channel,
            chat_id=ctx.msg.chat_id,
            content=content,
            metadata={**dict(ctx.msg.metadata or {}), "render_as": "text"},
        )
    if args[0].lower() == "schedule":
        cron = ctx.loop.cron_service
        if cron is None:
            content = "nanobot 原生 Cron 不可用；Skill 改进定时任务已停用。"
        else:
            batch_id = scheduled_batch_id(workspace, project_id, user_id)
            batch = store.get_scheduled_batch(batch_id)
            action = args[1].lower() if len(args) > 1 else "status"
            if action == "status":
                if batch is None:
                    content = "尚未配置 Skill 改进定时任务（默认关闭）。"
                else:
                    job = cron.get_job(batch.job_id)
                    job_state = (
                        f"任务已启用={job.enabled}，下次运行时间戳={job.state.next_run_at_ms}"
                        if job is not None
                        else "Cron 任务缺失"
                    )
                    content = (
                        f"Skill 改进定时任务：已启用={batch.enabled}，"
                        f"状态={_skill_status_label(batch.status)}，"
                        f"{job_state}；运行次数={batch.run_count}，"
                        f"最近错误={batch.last_error or '无'}。"
                    )
            elif action == "disable":
                if not ctx.is_user_turn:
                    content = "定时任务变更必须由用户在聊天中直接发起。"
                elif batch is None:
                    content = "尚未配置 Skill 改进定时任务。"
                else:
                    try:
                        cancelled = await ctx.loop._cancel_active_tasks(  # pyright: ignore[reportPrivateUsage]
                            f"devpilot:skill-batch:{batch.batch_id}"
                        )
                        store.set_scheduled_batch_enabled(batch_id, enabled=False)
                        cron.enable_job(batch.job_id, enabled=False)
                        content = (
                            "Skill 改进定时任务已停用，不会启动新的批次。"
                            f"已取消进行中的任务：{cancelled}。"
                        )
                    except (ValueError, OSError) as exc:
                        content = f"停用定时任务被拒绝：{_skill_exception_label(exc)}。"
            elif action == "run":
                if not ctx.is_user_turn:
                    content = "手动触发定时任务必须由用户在聊天中直接发起。"
                elif batch is None:
                    content = "尚未配置 Skill 改进定时任务，请先启用。"
                else:
                    try:
                        ran = await cron.run_job(batch.job_id, force=True)
                        content = (
                            "手动 Skill 改进批次已通过 nanobot 原生 Cron 排队。"
                            if ran
                            else "手动 Skill 改进批次已跳过，请检查定时任务状态。"
                        )
                    except (ValueError, OSError) as exc:
                        content = f"手动触发定时任务被拒绝：{_skill_exception_label(exc)}。"
            elif action == "enable":
                if not ctx.is_user_turn:
                    content = "定时任务变更必须由用户在聊天中直接发起。"
                else:
                    try:
                        schedule_values = shlex.split(ctx.args)[2:]
                    except ValueError:
                        schedule_values = []
                    parsed = _parse_skill_batch_schedule(schedule_values)
                    if isinstance(parsed, str):
                        content = parsed
                    else:
                        schedule, options, _timezone = parsed
                        new_job = False
                        current_scope = (
                            ctx.loop.workspace_scopes.for_message(
                                ctx.msg,
                                ctx.session.metadata if ctx.session is not None else {},
                            )
                            if ctx.session is not None
                            else None
                        )
                        batch_origin_metadata = {
                            "devpilot_skill_batch": True,
                            "batch_id": batch_id,
                            "workspace": str(workspace),
                            "workspace_scope": (
                                current_scope.metadata()
                                if current_scope is not None
                                else {
                                    "project_path": str(workspace),
                                    "access_mode": "full",
                                }
                            ),
                        }
                        job = cron.get_job(batch.job_id) if batch is not None else None
                        if job is None:
                            for candidate_job in cron.list_jobs(include_disabled=True):
                                marker = candidate_job.payload.origin_metadata.get(
                                    "devpilot_skill_batch"
                                )
                                marker_batch_id = candidate_job.payload.origin_metadata.get(
                                    "batch_id"
                                )
                                if marker is True and marker_batch_id == batch_id:
                                    job = candidate_job
                                    break
                        try:
                            if job is None:
                                job = cron.add_job(
                                    name="DevFlow Skill 改进",
                                    schedule=schedule,
                                    message="执行已配置的 DevFlow Skill 改进批次。",
                                    session_key=ctx.key,
                                    origin_channel=ctx.msg.channel,
                                    origin_chat_id=ctx.msg.chat_id,
                                    origin_metadata=batch_origin_metadata,
                                )
                                new_job = True
                            else:
                                job.payload.origin_metadata = batch_origin_metadata
                                updated_job = cron.update_job(
                                    job.id,
                                    schedule=schedule,
                                    message="执行已配置的 DevFlow Skill 改进批次。",
                                )
                                if isinstance(updated_job, str):
                                    raise ValueError(f"cron job update failed: {updated_job}")
                                job = updated_job
                            configured = store.configure_scheduled_batch(
                                batch_id=batch_id,
                                job_id=job.id,
                                workspace=workspace,
                                project_id=project_id,
                                user_id=user_id,
                                session_key=ctx.key,
                                enabled=True,
                                max_model_calls=options["calls"],
                                max_tokens=options["tokens"],
                                max_duration_seconds=options["timeout"],
                            )
                            cron.enable_job(job.id, enabled=True)
                            schedule_description = (
                                f"每 {schedule.every_ms // 1000} 秒"
                                if schedule.kind == "every" and schedule.every_ms
                                else f'Cron "{schedule.expr}"'
                            )
                            content = (
                                f"Skill 改进定时任务已启用：{schedule_description} "
                                f"（时区={_timezone}）；批次={configured.batch_id}。"
                                "系统只处理已验证证据，候选仍需人工审核。"
                            )
                        except (ValueError, OSError) as exc:
                            if new_job and job is not None:
                                cron.remove_job(job.id)
                            content = f"启用定时任务被拒绝：{_skill_exception_label(exc)}。"
            else:
                content = (
                    "用法：/skill-improvement schedule status|run|disable|"
                    "enable every <秒数> [时区] 或 cron \"<表达式>\" [时区] "
                    "[calls=1|2] "
                    "[tokens=512-20000] [timeout=30-600]"
                )
        return OutboundMessage(
            channel=ctx.msg.channel,
            chat_id=ctx.msg.chat_id,
            content=content,
            metadata={**dict(ctx.msg.metadata or {}), "render_as": "text"},
        )
    if args[0].lower() == "feedback":
        if not ctx.is_user_turn:
            content = "提交反馈必须由用户在聊天中直接发起。"
        else:
            feedback_text = ctx.args.strip()[len("feedback"):].strip()
            task_context = (ctx.session.metadata or {}).get("task_context")
            if not feedback_text:
                content = "用法：/skill-improvement feedback <问题描述>"
            elif (
                not isinstance(task_context, dict)
                or not isinstance(task_context.get("task_id"), str)
                or not str(task_context.get("profile_binding", "")).startswith("bug-fix@")
            ):
                content = "提交反馈前必须存在正在执行的 bug-fix 任务。"
            else:
                try:
                    skill = store.read_effective_snapshot(workspace)
                    evidence = store.record_feedback(
                        task_id=task_context["task_id"],
                        project_id=project_id,
                        workspace=workspace,
                        user_id=user_id,
                        session_id=ctx.key,
                        source_type="user_feedback",
                        skill_id="bug-fix",
                        skill_hash=skill.content_hash,
                        correction=feedback_text,
                        actual_result=feedback_text,
                    )
                    content = f"反馈已记录为已验证证据：`{evidence.evidence_id}`。"
                except (ValueError, OSError) as exc:
                    content = f"反馈被拒绝：{_skill_exception_label(exc)}。"
        return OutboundMessage(
            channel=ctx.msg.channel,
            chat_id=ctx.msg.chat_id,
            content=content,
            metadata={**dict(ctx.msg.metadata or {}), "render_as": "text"},
        )

    if args[0].lower() == "delete-evidence":
        if len(args) != 2:
            content = "用法：/skill-improvement delete-evidence <证据 ID>"
        elif not ctx.is_user_turn:
            content = "删除证据必须由用户在聊天中直接发起。"
        else:
            try:
                evidence = store.get_feedback(
                    args[1], workspace=workspace, user_id=user_id
                )
                if evidence is None:
                    raise ValueError("evidence not found")
                deleted = store.delete_feedback(
                    args[1],
                    workspace=workspace,
                    user_id=user_id,
                    project_id=project_id,
                )
                content = (
                    f"证据 `{deleted.evidence_id}` 已删除；关联候选已失效。"
                )
            except (ValueError, OSError) as exc:
                content = f"删除证据被拒绝：{_skill_exception_label(exc)}。"
        return OutboundMessage(
            channel=ctx.msg.channel,
            chat_id=ctx.msg.chat_id,
            content=content,
            metadata={**dict(ctx.msg.metadata or {}), "render_as": "text"},
        )

    candidate = store.get_candidate(
        args[0], workspace=workspace, user_id=user_id, project_id=project_id
    )
    if candidate is None:
        content = f"未找到候选 `{args[0]}`。"
    elif len(args) == 1 or args[1].lower() == "view":
        evaluation = store.get_evaluation(candidate.result_refs[-1]) if candidate.result_refs else None
        lines = [
            f"## Skill 候选 `{candidate.candidate_id}`",
            "",
            f"- Skill：`{candidate.skill_id}`",
            f"- 状态：`{_skill_status_label(candidate.status)}`",
            f"- 原因：{candidate.reason}",
            f"- 预期行为：{candidate.expected_behavior}",
            f"- 证据：{', '.join(candidate.evidence_ids)}",
        ]
        evidence_summaries = []
        for evidence_id in candidate.evidence_ids:
            evidence = store.get_feedback(
                evidence_id,
                workspace=workspace,
                user_id=user_id,
                project_id=project_id,
            )
            if evidence is not None:
                evidence_summaries.append(f"- 证据摘要：{evidence.correction[:240]}")
        lines.extend(evidence_summaries)
        baseline = store.read_effective_snapshot(workspace)
        diff = "".join(
            difflib.unified_diff(
                baseline.content.splitlines(keepends=True),
                candidate.candidate_content.splitlines(keepends=True),
                fromfile="active/skills/bug-fix/SKILL.md",
                tofile=f"candidate/{candidate.candidate_id}/SKILL.md",
            )
        )
        lines.extend(["", "候选 Diff 摘要：", "```diff", diff[:8_000], "```"])
        if evaluation is not None:
            lines.extend([
                f"- 固定对照：基线 {evaluation.baseline_successes}/9，"
                f"候选 {evaluation.candidate_successes}/9",
                f"- 资格状态：`{_skill_status_label(evaluation.status)}`",
            ])
        lines.extend([
            "",
            "操作：`/skill-improvement <id> enable|ignore|restore`。",
            "启用和恢复必须由用户在聊天中直接确认，不需要单独页面。",
        ])
        content = "\n".join(lines)
    elif args[1].lower() == "evaluate":
        try:
            report = store.evaluate_candidate(
                candidate.candidate_id,
                workspace=workspace,
                user_id=user_id,
                project_id=candidate.project_id,
            )
            content = (
                f"P8-02 固定对照已完成：基线 "
                f"{report.baseline_successes}/9，候选 {report.candidate_successes}/9；"
                f"状态为 `{_skill_status_label(report.status)}`。"
            )
        except (ValueError, OSError) as exc:
            content = f"评测被拒绝：{_skill_exception_label(exc)}。"
    elif args[1].lower() == "ignore":
        try:
            ignored = store.ignore_candidate(
                candidate.candidate_id,
                workspace=workspace,
                user_id=user_id,
                project_id=project_id,
            )
            content = f"候选 `{ignored.candidate_id}` 已忽略；当前生效的 Skill 未改变。"
        except (ValueError, OSError) as exc:
            content = f"忽略候选被拒绝：{_skill_exception_label(exc)}。"
    elif args[1].lower() == "enable":
        if not ctx.is_user_turn:
            content = "启用候选必须由用户在聊天中直接发起。"
        else:
            try:
                confirmation = store.issue_confirmation(
                    candidate.candidate_id,
                    user_id=user_id,
                    session_id=ctx.key,
                    workspace=workspace,
                    project_id=project_id,
                )
                active = store.activate_candidate(
                    confirmation.confirmation_id,
                    workspace=workspace,
                    user_id=user_id,
                    session_id=ctx.key,
                    project_id=project_id,
                )
                content = f"候选已对新任务启用。当前 Skill 哈希：`{active.content_hash}`。"
            except (ValueError, OSError) as exc:
                content = f"启用候选被拒绝：{_skill_exception_label(exc)}。"
    elif args[1].lower() == "restore":
        if not ctx.is_user_turn:
            content = "恢复候选必须由用户在聊天中直接发起。"
        else:
            try:
                restored = store.restore_candidate(
                    candidate.candidate_id,
                    workspace=workspace,
                    user_id=user_id,
                    project_id=project_id,
                )
                content = f"稳定版 Skill 已恢复。当前哈希：`{restored.content_hash}`。"
            except (ValueError, OSError) as exc:
                content = f"恢复候选被拒绝：{_skill_exception_label(exc)}。"
    else:
        content = (
            "用法：/skill-improvement <候选 ID> [view|enable|ignore|restore]，"
            "或 delete-evidence <证据 ID>"
        )
    return OutboundMessage(
        channel=ctx.msg.channel,
        chat_id=ctx.msg.chat_id,
        content=content,
        metadata={**dict(ctx.msg.metadata or {}), "render_as": "text"},
    )


async def cmd_trigger(ctx: CommandContext) -> OutboundMessage:
    """Create a local trigger bound to the current session."""
    name = ctx.args.strip()
    if not name:
        return OutboundMessage(
            channel=ctx.msg.channel,
            chat_id=ctx.msg.chat_id,
            content=(
                "Usage: /trigger <name>\n\n"
                "Create a named local trigger bound to this chat session."
            ),
            metadata={**dict(ctx.msg.metadata or {}), "render_as": "text"},
        )

    from nanobot.triggers.local_store import LocalTriggerStore

    loop = ctx.loop
    store = loop.local_trigger_store
    if store is None:
        store = LocalTriggerStore(loop.workspace)

    from nanobot.session.keys import UNIFIED_SESSION_KEY

    session_key = (
        ctx.msg.session_key
        if ctx.key == UNIFIED_SESSION_KEY
        else ctx.key
    )
    trigger = store.create(
        name=name,
        channel=ctx.msg.channel,
        chat_id=ctx.msg.chat_id,
        session_key=session_key,
        sender_id="trigger",
        origin_metadata=dict(ctx.msg.metadata or {}),
    )
    command = f'nanobot trigger {trigger.id} "message"'
    return OutboundMessage(
        channel=ctx.msg.channel,
        chat_id=ctx.msg.chat_id,
        content=(
            f"Trigger created: {trigger.name}\n"
            f"ID: {trigger.id}\n\n"
            f"Command:\n{command}"
        ),
        metadata={**dict(ctx.msg.metadata or {}), "render_as": "text"},
    )

async def cmd_help(ctx: CommandContext) -> OutboundMessage:
    """Return available slash commands."""
    return OutboundMessage(
        channel=ctx.msg.channel,
        chat_id=ctx.msg.chat_id,
        content=build_help_text(),
        metadata={**dict(ctx.msg.metadata or {}), "render_as": "text"},
    )


def build_help_text() -> str:
    """Build canonical help text shared across channels."""
    lines = ["🐈 nanobot commands:"]
    for spec in BUILTIN_COMMAND_SPECS:
        command = spec.command
        if spec.arg_hint:
            command = f"{command} {spec.arg_hint}"
        lines.append(f"{command} — {spec.description}")
    return "\n".join(lines)


def register_builtin_commands(router: CommandRouter) -> None:
    """Register the default set of slash commands."""
    router.priority("/stop", cmd_stop)
    router.priority("/restart", cmd_restart)
    router.priority("/status", cmd_status)
    router.exact("/new", cmd_new)
    router.exact("/status", cmd_status)
    router.exact("/model", cmd_model)
    router.prefix("/model ", cmd_model)
    router.exact("/task-profile", cmd_task_profile)
    router.prefix("/task-profile ", cmd_task_profile)
    router.exact("/history", cmd_history)
    router.prefix("/history ", cmd_history)
    router.exact("/goal", cmd_goal)
    router.prefix("/goal ", cmd_goal)
    router.exact("/trigger", cmd_trigger)
    router.prefix("/trigger ", cmd_trigger)
    router.exact("/dream", cmd_dream)
    router.exact("/dream-log", cmd_dream_log)
    router.prefix("/dream-log ", cmd_dream_log)
    router.exact("/dream-restore", cmd_dream_restore)
    router.prefix("/dream-restore ", cmd_dream_restore)
    router.exact("/dream-prompt", cmd_dream_prompt)
    router.prefix("/dream-prompt ", cmd_dream_prompt)
    router.exact("/evaluator-prompt", cmd_evaluator_prompt)
    router.prefix("/evaluator-prompt ", cmd_evaluator_prompt)
    router.exact("/skill", cmd_skill)
    router.exact("/skill-improvement", cmd_skill_improvement)
    router.prefix("/skill-improvement ", cmd_skill_improvement)
    router.exact("/help", cmd_help)
    router.exact("/pairing", cmd_pairing)
    router.prefix("/pairing ", cmd_pairing)
