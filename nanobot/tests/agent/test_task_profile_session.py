"""Session isolation tests for developer-owned task profiles."""

from unittest.mock import MagicMock

from nanobot.agent.loop import AgentLoop
from nanobot.bus.queue import MessageBus
from nanobot.providers.base import GenerationSettings


def _loop(tmp_path) -> AgentLoop:
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    provider.generation = GenerationSettings()
    return AgentLoop(
        bus=MessageBus(),
        provider=provider,
        workspace=tmp_path,
        model="test-model",
        context_window_tokens=1000,
    )


def test_task_profile_binding_is_scoped_to_one_session(tmp_path) -> None:
    loop = _loop(tmp_path)
    loop.task_profiles.validate_runtime = lambda *_args: None  # type: ignore[method-assign]

    profile = loop.set_session_task_profile("cli:one", "feature-delivery")
    first = loop.sessions.get_or_create("cli:one")
    second = loop.sessions.get_or_create("cli:two")

    assert profile.name == "feature-delivery"
    assert first.metadata["task_profile"] == "feature-delivery"
    assert first.metadata["task_profile_version"] == "1"
    assert first.policy.allowed_tools == profile.allowed_tools
    assert second.metadata.get("task_profile") is None
    assert second.policy.allowed_tools is None


def test_task_profile_version_mismatch_is_rejected(tmp_path) -> None:
    loop = _loop(tmp_path)
    loop.task_profiles.validate_runtime = lambda *_args: None  # type: ignore[method-assign]
    loop.set_session_task_profile("cli:one", "repo-analysis")
    session = loop.sessions.get_or_create("cli:one")
    session.metadata["task_profile_version"] = "old"

    try:
        loop._sync_session_task_profile(session)
    except ValueError as exc:
        assert "is stale" in str(exc)
    else:
        raise AssertionError("a stale task-profile binding must be rejected")
