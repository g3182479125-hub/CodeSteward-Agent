"""Session isolation tests for developer-owned task profiles."""

import hashlib
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


def test_task_profile_cannot_be_replaced_in_an_unfinished_session(tmp_path) -> None:
    loop = _loop(tmp_path)
    loop.task_profiles.validate_runtime = lambda *_args: None  # type: ignore[method-assign]
    loop.set_session_task_profile("cli:one", "repo-analysis")
    session = loop.sessions.get_or_create("cli:one")
    session.add_message("user", "unfinished task")
    loop.sessions.save(session)

    try:
        loop.set_session_task_profile("cli:one", "feature-delivery")
    except ValueError as exc:
        assert "use /new" in str(exc)
    else:
        raise AssertionError("an unfinished session must keep its profile binding")


def test_task_context_is_bound_to_profile_and_project_identity(tmp_path) -> None:
    loop = _loop(tmp_path)
    loop.task_profiles.validate_runtime = lambda *_args: None  # type: ignore[method-assign]
    profile = loop.set_session_task_profile("cli:one", "feature-delivery")
    session = loop.sessions.get_or_create("cli:one")

    state = loop._ensure_task_context(session, profile, "implement the parser", tmp_path)

    assert state.profile_binding == "feature-delivery@1"
    assert state.session_ref
    assert state.project_ref
    assert "implement the parser" in state.prompt_text()
    assert session.metadata["task_context"]["project_ref"] == state.project_ref


def test_task_context_marks_external_file_change_before_new_proposal(tmp_path) -> None:
    loop = _loop(tmp_path)
    loop.task_profiles.validate_runtime = lambda *_args: None  # type: ignore[method-assign]
    profile = loop.set_session_task_profile("cli:one", "feature-delivery")
    session = loop.sessions.get_or_create("cli:one")
    tracked = tmp_path / "src.py"
    tracked.write_text("return 1", encoding="utf-8")
    digest = hashlib.sha256(tracked.read_bytes()).hexdigest()
    state = loop._ensure_task_context(session, profile, "fix src", tmp_path)
    loop.update_task_context_evidence("cli:one", file_hashes=(("src.py", digest),))

    tracked.write_text("return 2", encoding="utf-8")
    refreshed = loop._ensure_task_context(session, profile, "fix src", tmp_path)

    assert "external file change detected" in refreshed.next_step or "external file change detected" in " ".join(refreshed.constraints)
    assert refreshed.project_ref == state.project_ref


def test_task_context_records_real_devpilot_evidence_results(tmp_path) -> None:
    loop = _loop(tmp_path)
    loop.task_profiles.validate_runtime = lambda *_args: None  # type: ignore[method-assign]
    profile = loop.set_session_task_profile("cli:one", "feature-delivery")
    session = loop.sessions.get_or_create("cli:one")
    loop._ensure_task_context(session, profile, "fix src", tmp_path)

    loop._record_task_context_tool_result(
        session,
        profile,
        "devpilot_read_code",
        {"project_id": "project-1", "evidence_run_id": "run-1"},
        '{"path":"src.py","sha256":"' + "a" * 64 + '"}',
    )

    stored = session.metadata["task_context"]
    assert stored["evidence_refs"] == ["run-1"]
    assert stored["file_hashes"] == [{"path": "src.py", "sha256": "a" * 64}]


def test_task_context_records_proposal_change_set_and_evidence(tmp_path) -> None:
    loop = _loop(tmp_path)
    loop.task_profiles.validate_runtime = lambda *_args: None  # type: ignore[method-assign]
    profile = loop.set_session_task_profile("cli:one", "feature-delivery")
    session = loop.sessions.get_or_create("cli:one")
    loop._ensure_task_context(session, profile, "add feature", tmp_path)

    loop._record_task_context_tool_result(
        session,
        profile,
        "devpilot_feature_delivery_propose",
        {"evidence_ids": ["analysis-1"]},
        '{"change_set_id":"changeset-1"}',
    )

    stored = session.metadata["task_context"]
    assert stored["evidence_refs"] == ["analysis-1"]
    assert stored["change_set_id"] == "changeset-1"
