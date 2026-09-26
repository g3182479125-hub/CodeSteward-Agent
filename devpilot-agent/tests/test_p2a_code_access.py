from __future__ import annotations

import asyncio
import hashlib
import json
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from nanobot.agent.tools.context import RequestContext, bind_request_context, reset_request_context

from devpilot import nanobot_tools
from devpilot.code_access import CodeAccessError, CodeAccessService
from devpilot.nanobot_tools import InspectProjectTool, ReadCodeTool, SearchCodeTool, ToolResult
from devpilot.paths import AllowedRoots
from devpilot.storage import ProjectStore


def make_service(tmp_path: Path) -> tuple[CodeAccessService, Path, str]:
    project = tmp_path / "project"
    project.mkdir()
    store = ProjectStore(tmp_path / "data" / "devpilot.db")
    store.initialize()
    project_id = store.ensure_project(str(project.resolve()))
    service = CodeAccessService(
        allowed_roots=AllowedRoots.from_paths([tmp_path]),
        store=store,
    )
    return service, project, project_id


def test_read_returns_line_range_hash_and_redacts_values(tmp_path: Path) -> None:
    service, project, project_id = make_service(tmp_path)
    content = "first\napi_key = super-secret-value\nthird\n"
    (project / "main.py").write_bytes(content.encode())

    result = service.read(project_id, "main.py", start_line=2, end_line=3)

    assert result.path == "main.py"
    assert result.start_line == 2
    assert result.end_line == 3
    assert "super-secret-value" not in result.content
    assert "[REDACTED]" in result.content
    assert result.sha256 == hashlib.sha256(content.encode()).hexdigest()
    assert result.truncated is False


def test_search_is_bounded_filters_glob_and_skips_secrets(tmp_path: Path) -> None:
    service, project, project_id = make_service(tmp_path)
    (project / "src").mkdir()
    (project / "src" / "app.py").write_text("Needle\nneedle again\n", encoding="utf-8")
    (project / "README.md").write_text("needle in docs\n", encoding="utf-8")
    (project / ".env").write_text("needle=secret\n", encoding="utf-8")

    result = service.search(project_id, "needle", glob="src/*.py", limit=1)

    assert len(result.matches) == 1
    assert result.matches[0].path == "src/app.py"
    assert result.truncated is True
    assert all(match.path != ".env" for match in result.matches)


def test_source_names_containing_token_are_not_misclassified(tmp_path: Path) -> None:
    service, project, project_id = make_service(tmp_path)
    (project / "tokenizer.py").write_text("def tokenize():\n    return 'ok'\n", encoding="utf-8")

    result = service.search(project_id, "tokenize")

    assert result.matches[0].path == "tokenizer.py"


def test_read_public_password_template_keeps_secret_paths_blocked(tmp_path: Path) -> None:
    service, project, project_id = make_service(tmp_path)
    templates = project / "shop" / "templates"
    templates.mkdir(parents=True)
    (templates / "forgot_password.html").write_text("<form>Reset</form>\n", encoding="utf-8")
    (templates / "secret_password.html").write_text("private\n", encoding="utf-8")
    (project / "forgot_password.html").write_text("private\n", encoding="utf-8")

    result = service.read(project_id, "shop/templates/forgot_password.html")
    assert "<form>Reset</form>" in result.content
    with pytest.raises(CodeAccessError) as error:
        service.read(project_id, "shop/templates/secret_password.html")
    assert error.value.code == "FORBIDDEN_FILE"
    with pytest.raises(CodeAccessError) as error:
        service.read(project_id, "forgot_password.html")
    assert error.value.code == "FORBIDDEN_FILE"


@pytest.mark.parametrize(
    ("path", "code"),
    [
        ("../outside.py", "PATH_OUTSIDE_PROJECT"),
        (".env", "FORBIDDEN_FILE"),
        ("CON.txt", "WINDOWS_PATH_ALIAS"),
        ("main.py. ", "WINDOWS_PATH_ALIAS"),
        ("main.py:stream", "WINDOWS_PATH_ALIAS"),
    ],
)
def test_read_rejects_unsafe_paths(tmp_path: Path, path: str, code: str) -> None:
    service, _project, project_id = make_service(tmp_path)

    with pytest.raises(CodeAccessError) as error:
        service.read(project_id, path)

    assert error.value.code == code


def test_read_rejects_binary_unknown_project_and_symlink(tmp_path: Path) -> None:
    service, project, project_id = make_service(tmp_path)
    (project / "binary.dat").write_bytes(b"\x00\x01")
    with pytest.raises(CodeAccessError) as error:
        service.read(project_id, "binary.dat")
    assert error.value.code == "BINARY_FILE_FORBIDDEN"

    with pytest.raises(CodeAccessError) as error:
        service.read("unknown", "binary.dat")
    assert error.value.code == "PROJECT_NOT_AVAILABLE"

    outside = tmp_path / "outside.py"
    outside.write_text("outside\n", encoding="utf-8")
    link = project / "link.py"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlink creation unavailable: {exc}")
    with pytest.raises(CodeAccessError) as error:
        service.read(project_id, "link.py")
    assert error.value.code in {"PATH_OUTSIDE_PROJECT", "TARGET_NOT_FILE"}


def test_nanobot_search_and_read_tools_use_registered_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "main.py").write_text("def hello():\n    return 'hello'\n", encoding="utf-8")
    data_dir = tmp_path / "data"
    store = ProjectStore(data_dir / "devpilot.db")
    store.initialize()
    project_id = store.ensure_project(str(project.resolve()))
    monkeypatch.setenv("DEVPILOT_DATA_DIR", str(data_dir))
    monkeypatch.setenv("DEVPILOT_ALLOWED_ROOTS", str(tmp_path))

    search_raw = asyncio.run(SearchCodeTool(tmp_path).execute(project_id=project_id, query="hello"))
    read_raw = asyncio.run(ReadCodeTool(tmp_path).execute(project_id=project_id, path="main.py"))

    assert not isinstance(search_raw, ToolResult) or not search_raw.is_error
    assert json.loads(str(search_raw))["matches"][0]["path"] == "main.py"
    assert not isinstance(read_raw, ToolResult) or not read_raw.is_error
    assert "def hello" in json.loads(str(read_raw))["content"]

    denied = asyncio.run(ReadCodeTool(tmp_path).execute(project_id=project_id, path="../outside"))
    assert isinstance(denied, ToolResult)
    assert denied.is_error is True
    assert "PATH_DENIED" in str(denied)


def test_code_tools_reuse_current_turn_repo_analysis_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A normal natural-language turn need not restate the inspect run ID."""

    project = tmp_path / "project"
    project.mkdir()
    data_dir = tmp_path / "data"
    monkeypatch.setenv("DEVPILOT_DATA_DIR", str(data_dir))
    monkeypatch.setenv("DEVPILOT_ALLOWED_ROOTS", str(tmp_path))
    captured: list[str | None] = []

    class Access:
        def read(self, _project_id: str, _path: str, **kwargs: object) -> SimpleNamespace:
            captured.append(kwargs.get("evidence_run_id"))
            return SimpleNamespace(to_dict=lambda: {"path": "main.py", "sha256": "a" * 64})

    monkeypatch.setattr(nanobot_tools, "_code_access_service", lambda _workspace: Access())
    context = RequestContext(
        channel="websocket",
        chat_id="chat",
        attributes={
            "devpilot_repo_analysis": {"project_id": "project-1", "run_id": "analysis-1"}
        },
    )
    token = bind_request_context(context)
    try:
        result = asyncio.run(
            ReadCodeTool(tmp_path).execute(project_id="project-1", path="main.py")
        )
    finally:
        reset_request_context(token)

    assert not isinstance(result, ToolResult) or not result.is_error
    assert captured == ["analysis-1"]


def test_inspect_tool_does_not_block_the_agent_event_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A slow local inspection must not freeze WebUI health or other chats."""

    inspect_started = threading.Event()
    allow_inspect_to_finish = threading.Event()
    callback_processed_before_finish = threading.Event()

    class SlowService:
        def inspect(self, _project_path: Path) -> SimpleNamespace:
            inspect_started.set()
            assert allow_inspect_to_finish.wait(timeout=1)
            return SimpleNamespace(
                project_id="project-id",
                run_id="run-id",
                inspection=SimpleNamespace(to_dict=lambda: {"files_seen": 1}),
            )

    monkeypatch.setattr(nanobot_tools, "_service", lambda _workspace: SlowService())

    async def run() -> str | ToolResult:
        loop = asyncio.get_running_loop()
        callback_ready = threading.Event()

        def mark_callback() -> None:
            callback_ready.set()

        def release_after_callback() -> None:
            assert inspect_started.wait(timeout=1)
            loop.call_soon_threadsafe(mark_callback)
            if callback_ready.wait(timeout=0.5):
                callback_processed_before_finish.set()
            allow_inspect_to_finish.set()

        watchdog = threading.Thread(target=release_after_callback, daemon=True)
        watchdog.start()
        task = asyncio.create_task(InspectProjectTool(tmp_path).execute(project_path=str(tmp_path)))
        result = await task
        watchdog.join(timeout=1)
        return result

    result = asyncio.run(run())

    assert callback_processed_before_finish.is_set()
    assert not isinstance(result, ToolResult) or not result.is_error
