"""Native chat commands for the H3 trusted plugin manager."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from nanobot.agent.task_profiles import DynamicPluginPolicy
from nanobot.bus.events import OutboundMessage
from nanobot.command.router import CommandContext, CommandRouter

if TYPE_CHECKING:
    from nanobot.agent.task_profiles import TaskProfile


def _text(ctx: CommandContext, content: str) -> OutboundMessage:
    return OutboundMessage(
        channel=ctx.msg.channel,
        chat_id=ctx.msg.chat_id,
        content=content,
        metadata={**dict(ctx.msg.metadata or {}), "render_as": "text"},
    )


def _project_id(ctx: CommandContext) -> str:
    path = ctx.workspace or ctx.loop.workspace
    return ctx.loop.plugin_manager.project_id_for_path(path)


def _profile_policy(ctx: CommandContext) -> tuple[TaskProfile | None, DynamicPluginPolicy | None]:
    session = ctx.session or ctx.loop.sessions.get_or_create(ctx.key)
    profile = ctx.loop.task_profile_for_session(session)
    return profile, profile.dynamic_plugins if profile is not None else None


def _usage() -> str:
    return (
        "Usage: `/plugins list`; `/plugins load <plugin-id>@<version>`; "
        "`/plugins update <plugin-id>@<version> [operation-id] [expected-generation]`; "
        "`/plugins stop <plugin-id> [operation-id] [expected-generation]`; "
        "`/plugins remove <plugin-id>@<version> [operation-id] [expected-generation]`; "
        "`/plugins switch-loop <default-react|plan-then-act>`; "
        "`/plugins cleanup-retry <operation-id>`; "
        "`/plugins status <operation-id>`"
    )


def _format_list(rows: tuple[dict[str, Any], ...]) -> str:
    if not rows:
        return "No registered plugin versions are visible in this scope."
    lines = ["Registered plugins:"]
    for row in rows:
        line = (
            f"- `{row['plugin_id']}@{row['version']}` "
            f"[{row['lifecycle']}] scope={row['scope']} source={row['source']}"
        )
        if "tool" in row:
            line += f" tool={row['tool']} generation={row['generation']}"
        lines.append(line)
    return "\n".join(lines)


def _format_result(result: Any) -> str:
    operation_line = (
        f"\n- operation_id: `{result.operation_id}`"
        if result.operation_id is not None
        else ""
    )
    if result.ok:
        details = result.details_dict
        suffix = ""
        if details:
            suffix = "\n" + "\n".join(f"- {key}: {value}" for key, value in details.items())
        return f"{result.message}.{operation_line}{suffix}"
    code = result.code.value if result.code is not None else "unknown"
    return f"Plugin operation failed [{code}]: {result.message}.{operation_line}"


async def cmd_plugins(ctx: CommandContext) -> OutboundMessage:
    """List and control developer-registered H2 tool versions."""
    manager = ctx.loop.plugin_manager
    args = ctx.args.strip()
    project_id = _project_id(ctx)
    if not args:
        return _text(ctx, _usage())
    parts = args.split()
    action = parts[0].lower()
    if action == "list" and len(parts) == 1:
        return _text(ctx, _format_list(manager.list_view(ctx.key, project_id=project_id)))
    if action == "status" and len(parts) == 2:
        status = manager.operation_status(
            parts[1],
            session_id=ctx.key,
            project_id=project_id,
        )
        if status is not None:
            return _text(ctx, str(status))
        handoff = ctx.loop.loop_handoff_manager
        loop_status = handoff.operation_status(ctx.key, parts[1]) if handoff is not None else None
        if loop_status is not None:
            return _text(
                ctx,
                "Loop switch status: "
                f"{loop_status.status}; driver={loop_status.driver_name}@{loop_status.driver_version}; "
                f"generation={loop_status.owner_generation}",
            )
        return _text(ctx, "Unknown plugin or loop-switch operation.")
    if action == "switch-loop":
        if len(parts) != 2:
            return _text(ctx, "Usage: `/plugins switch-loop <default-react|plan-then-act>`")
        if not ctx.is_user_turn:
            return _text(ctx, "Only a direct user message can request a loop switch.")
        try:
            operation_id = ctx.loop.request_loop_driver_switch(ctx.key, parts[1])
        except (RuntimeError, ValueError) as exc:
            return _text(ctx, f"Loop switch was not accepted: {exc}")
        return _text(
            ctx,
            "Loop switch is pending at the next safe Host boundary. "
            f"operation_id: `{operation_id}`. The current task will not be restarted.",
        )
    if action not in {"load", "update", "stop", "remove", "cleanup-retry"}:
        return _text(ctx, _usage())
    if not ctx.is_user_turn:
        return _text(ctx, "Only a direct user message can change dynamic plugin routing.")
    _profile, policy = _profile_policy(ctx)
    if policy is not None and not policy.enabled:
        return _text(ctx, "Dynamic plugins are disabled by the active task profile.")
    manager.authorize(ctx.key, project_id)

    if action == "load":
        if len(parts) != 2:
            return _text(ctx, _usage())
        plugin_id, separator, version = parts[1].partition("@")
        if not separator or not plugin_id or not version:
            return _text(ctx, f"Usage: `/plugins {action} <plugin-id>@<version>`")
        result = manager.load(
            plugin_id,
            version,
            session_id=ctx.key,
            project_id=project_id,
            policy=policy,
        )
        return _text(ctx, _format_result(result))

    if action == "remove":
        if len(parts) not in {2, 3, 4}:
            return _text(ctx, _usage())
        plugin_id, separator, version = parts[1].partition("@")
        if not separator or not plugin_id or not version:
            return _text(ctx, "Usage: `/plugins remove <plugin-id>@<version> [operation-id] [expected-generation]`")
        remove_expected_generation: int | None = None
        if len(parts) == 4:
            try:
                remove_expected_generation = int(parts[3])
            except ValueError:
                return _text(ctx, "expected-generation 必须是整数。")
        result = manager.remove(
            plugin_id,
            version,
            session_id=ctx.key,
            project_id=project_id,
            operation_id=parts[2] if len(parts) >= 3 else None,
            expected_generation=remove_expected_generation,
            policy=policy,
        )
        return _text(ctx, _format_result(result))

    if action == "update":
        if len(parts) not in {2, 3, 4}:
            return _text(ctx, _usage())
        plugin_id, separator, version = parts[1].partition("@")
        if not separator or not plugin_id or not version:
            return _text(ctx, "Usage: `/plugins update <plugin-id>@<version> [operation-id] [expected-generation]`")
        expected_generation: int | None = None
        if len(parts) == 4:
            try:
                expected_generation = int(parts[3])
            except ValueError:
                return _text(ctx, "expected-generation 必须是整数。")
        result = await manager.update(
            plugin_id,
            version,
            session_id=ctx.key,
            project_id=project_id,
            operation_id=parts[2] if len(parts) >= 3 else None,
            expected_generation=expected_generation,
            policy=policy,
        )
        return _text(ctx, _format_result(result))

    if action == "cleanup-retry":
        if len(parts) != 2:
            return _text(ctx, _usage())
        result = await manager.retry_cleanup(
            parts[1],
            session_id=ctx.key,
            project_id=project_id,
        )
        return _text(ctx, _format_result(result))

    if len(parts) not in {2, 3, 4}:
        return _text(ctx, _usage())
    expected_generation = None
    if len(parts) == 4:
        try:
            expected_generation = int(parts[3])
        except ValueError:
            return _text(ctx, "expected-generation 必须是整数。")
    result = manager.stop(
        parts[1],
        session_id=ctx.key,
        project_id=project_id,
        operation_id=parts[2] if len(parts) >= 3 else None,
        expected_generation=expected_generation,
        policy=policy,
    )
    return _text(ctx, _format_result(result))


def register_plugin_commands(router: CommandRouter) -> None:
    """Attach H2 commands without changing the existing command handlers."""
    router.exact("/plugins", cmd_plugins)
    router.prefix("/plugins ", cmd_plugins)
