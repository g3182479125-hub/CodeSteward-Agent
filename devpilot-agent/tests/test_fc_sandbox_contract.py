from __future__ import annotations

import sys
from pathlib import Path
from threading import Event, Thread
from types import SimpleNamespace

import pytest

from devpilot.execution import (
    ExecutionError,
    ExecutionFile,
    ExecutionRequest,
    ExecutionResult,
    FakeExecutionBackend,
    request_from_mapping,
)
from devpilot.fc_sandbox import FCSandboxBackend, FCSandboxConfig


def valid_request(**overrides: object) -> ExecutionRequest:
    values: dict[str, object] = {
        "run_id": "run-1",
        "project_id": "project-1",
        "files": (ExecutionFile("src/main.py", b"print('ok')"),),
        "command": "python3 /workspace/devpilot/src/main.py",
    }
    values.update(overrides)
    return ExecutionRequest(**values)  # type: ignore[arg-type]


def test_fake_backend_keeps_execution_port_deterministic() -> None:
    backend = FakeExecutionBackend()
    result = backend.run(valid_request())

    assert result.backend == "fake"
    assert result.exit_code == 0
    assert backend.requests[0].files[0].relative_path == "src/main.py"


@pytest.mark.parametrize(
    "overrides, code",
    [
        ({"files": ()}, "EXECUTION_INPUT_EMPTY"),
        ({"command": "  "}, "EXECUTION_COMMAND_EMPTY"),
        ({"timeout_seconds": 901}, "EXECUTION_TIMEOUT_INVALID"),
        ({"files": (ExecutionFile("../escape.py", b"x"),)}, "EXECUTION_PATH_INVALID"),
        (
            {"files": (ExecutionFile("src/main.py", b"x"), ExecutionFile("src/main.py", b"y"))},
            "EXECUTION_PATH_DUPLICATE",
        ),
    ],
)
def test_execution_port_fails_closed(overrides: dict[str, object], code: str) -> None:
    with pytest.raises(ExecutionError, match=code):
        FakeExecutionBackend().run(valid_request(**overrides))


def test_fc_config_requires_environment_without_printing_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in ("E2B_API_KEY", "E2B_API_URL", "E2B_DOMAIN"):
        monkeypatch.delenv(name, raising=False)

    with pytest.raises(ExecutionError, match="FC_CONFIG_MISSING"):
        FCSandboxConfig.from_environment()


def test_mapping_parser_accepts_internal_bytes_only() -> None:
    request = request_from_mapping(
        {
            "run_id": "run-1",
            "project_id": "project-1",
            "command": "python3 /workspace/devpilot/src/main.py",
            "files": [{"relative_path": "src/main.py", "content": b"print(1)"}],
        }
    )
    assert request.files[0].content == b"print(1)"


@pytest.mark.parametrize("value", [True, "16384"])
def test_mapping_parser_rejects_non_integer_output_limit(value: object) -> None:
    with pytest.raises(ExecutionError, match="EXECUTION_INPUT_INVALID"):
        request_from_mapping(
            {
                "run_id": "run-1",
                "project_id": "project-1",
                "command": "python3 /workspace/devpilot/src/main.py",
                "files": [{"relative_path": "src/main.py", "content": b"print(1)"}],
                "max_output_bytes": value,
            }
        )


def test_fc_backend_uses_short_lived_sandbox_lifecycle(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[object] = []

    class FakeFiles:
        def make_dir(self, path: str) -> None:
            events.append(("mkdir", path))

        def write(self, path: str, content: bytes) -> None:
            events.append(("write", path, content))

    class FakeCommands:
        def run(self, command: str, *, timeout: int) -> SimpleNamespace:
            events.append(("run", command, timeout))
            return SimpleNamespace(exit_code=0, stdout="ok", stderr="")

    class FakeSandbox:
        def __init__(self) -> None:
            self.sandbox_id = "fc-test-sandbox"
            self.files = FakeFiles()
            self.commands = FakeCommands()

        def kill(self) -> None:
            events.append("kill")

    class FakeSandboxFactory:
        @classmethod
        def create(cls, **kwargs: object) -> FakeSandbox:
            events.append(("create", kwargs))
            return FakeSandbox()

    monkeypatch.setitem(
        sys.modules,
        "e2b_code_interpreter",
        SimpleNamespace(Sandbox=FakeSandboxFactory),
    )
    backend = FCSandboxBackend(
        FCSandboxConfig(
            api_key="test-key",
            api_url="https://api.example.invalid",
            domain="example.invalid",
        )
    )
    result = backend.run(valid_request())

    assert result.sandbox_id == "fc-test-sandbox"
    assert result.stdout == "ok"
    assert events[0][0] == "create"
    assert ("write", "/workspace/devpilot/src/main.py", b"print('ok')") in events
    assert events[-1] == "kill"
    assert backend.get_run_state("run-1")["status"] == "completed"


def test_fc_backend_bootstraps_pytest_before_project_test(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[tuple[object, ...]] = []

    class FakeFiles:
        def make_dir(self, path: str) -> None:
            events.append(("mkdir", path))

        def write(self, path: str, content: bytes) -> None:
            events.append(("write", path, content))

    class FakeCommands:
        def run(self, command: str, *, timeout: int, cwd: str | None = None) -> SimpleNamespace:
            events.append(("run", command, timeout, cwd))
            return SimpleNamespace(exit_code=0, stdout="ok", stderr="")

    class FakeSandbox:
        sandbox_id = "pytest-bootstrap-sandbox"
        files = FakeFiles()
        commands = FakeCommands()

        def kill(self) -> None:
            events.append(("kill",))

    class Factory:
        @classmethod
        def create(cls, **_kwargs: object) -> FakeSandbox:
            events.append(("create",))
            return FakeSandbox()

    monkeypatch.setitem(sys.modules, "e2b_code_interpreter", SimpleNamespace(Sandbox=Factory))

    result = FCSandboxBackend(
        FCSandboxConfig(
            api_key="test-key",
            api_url="https://api.example.invalid",
            domain="example.invalid",
            network_policy_mode="provider-default",
        )
    ).run(valid_request(command_profile="project-pytest", command="python -m pytest -q"))

    assert result.exit_code == 0
    run_events = [event for event in events if event[0] == "run"]
    assert run_events[0] == (
        "run",
        "python -m pip install --disable-pip-version-check pytest",
        120,
        None,
    )
    assert run_events[1] == ("run", "python -m pytest -q", 300, "/workspace/devpilot")


def test_fc_backend_prepares_server_selected_project_dependencies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[tuple[object, ...]] = []

    class FakeCommands:
        def run(self, command: str, *, timeout: int, cwd: str | None = None) -> SimpleNamespace:
            events.append((command, timeout, cwd))
            return SimpleNamespace(exit_code=0, stdout="ok", stderr="")

    class FakeSandbox:
        sandbox_id = "dependency-bootstrap-sandbox"
        files = SimpleNamespace(make_dir=lambda _path: None, write=lambda *_args: None)
        commands = FakeCommands()

        def kill(self) -> None:
            return None

    class Factory:
        @classmethod
        def create(cls, **_kwargs: object) -> FakeSandbox:
            return FakeSandbox()

    monkeypatch.setitem(sys.modules, "e2b_code_interpreter", SimpleNamespace(Sandbox=Factory))
    backend = FCSandboxBackend(
        FCSandboxConfig(
            api_key="test-key",
            api_url="https://api.example.invalid",
            domain="example.invalid",
            install_pytest=False,
            allow_network=True,
            network_policy_mode="provider-default",
        )
    )

    result = backend.run(
        valid_request(
            files=(ExecutionFile("src/requirements.txt", b"pytest==8.0.0\n"),),
            command="python -m pytest -q",
            command_profile="project-pytest",
            working_directory="src",
            dependency_profile="python-requirements",
        )
    )

    assert result.exit_code == 0
    assert events == [
        (
            "python -m pip install --disable-pip-version-check -r "
            "/workspace/devpilot/src/requirements.txt",
            300,
            "/workspace/devpilot/src",
        ),
        ("python -m pytest -q", 300, "/workspace/devpilot/src"),
    ]


def test_fc_backend_reports_pytest_bootstrap_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeCommands:
        def run(self, command: str, *, timeout: int, cwd: str | None = None) -> SimpleNamespace:
            assert command == "python -m pip install --disable-pip-version-check pytest"
            return SimpleNamespace(exit_code=1, stdout="", stderr="network unavailable")

    class FakeSandbox:
        sandbox_id = "pytest-bootstrap-failure-sandbox"
        files = SimpleNamespace(make_dir=lambda _path: None, write=lambda _path, _data: None)
        commands = FakeCommands()

        def kill(self) -> None:
            return None

    class Factory:
        @classmethod
        def create(cls, **_kwargs: object) -> FakeSandbox:
            return FakeSandbox()

    monkeypatch.setitem(sys.modules, "e2b_code_interpreter", SimpleNamespace(Sandbox=Factory))

    with pytest.raises(ExecutionError, match="FC_PYTEST_INSTALL_FAILED"):
        FCSandboxBackend(
            FCSandboxConfig(
                api_key="test-key",
                api_url="https://api.example.invalid",
                domain="example.invalid",
                network_policy_mode="provider-default",
            )
        ).run(valid_request(command_profile="project-pytest", command="python -m pytest -q"))


def test_fc_backend_runs_uploaded_project_from_its_sandbox_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, object] = {}

    class FakeSandbox:
        sandbox_id = "cwd-sandbox"
        files = SimpleNamespace(make_dir=lambda _path: None, write=lambda _path, _data: None)

        def kill(self) -> None:
            return None

    def run(command: str, *, timeout: int, cwd: str) -> SimpleNamespace:
        observed.update(command=command, timeout=timeout, cwd=cwd)
        return SimpleNamespace(exit_code=0, stdout="ok", stderr="")

    FakeSandbox.commands = SimpleNamespace(run=run)  # type: ignore[attr-defined]

    class Factory:
        @classmethod
        def create(cls, **_kwargs: object) -> FakeSandbox:
            return FakeSandbox()

    monkeypatch.setitem(sys.modules, "e2b_code_interpreter", SimpleNamespace(Sandbox=Factory))
    result = FCSandboxBackend(
        FCSandboxConfig(api_key="test-key", api_url="https://api.example.invalid", domain="invalid")
    ).run(valid_request())

    assert result.exit_code == 0
    assert observed["cwd"] == "/workspace/devpilot"


def test_fc_backend_rejects_duplicate_run_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeSandboxFactory:
        @classmethod
        def create(cls, **kwargs: object) -> object:
            return SimpleNamespace(
                sandbox_id="duplicate-sandbox",
                files=SimpleNamespace(make_dir=lambda _path: None, write=lambda _path, _data: None),
                commands=SimpleNamespace(
                    run=lambda _command, timeout: SimpleNamespace(
                        exit_code=0, stdout="ok", stderr=""
                    )
                ),
                kill=lambda: None,
            )

    monkeypatch.setitem(
        sys.modules,
        "e2b_code_interpreter",
        SimpleNamespace(Sandbox=FakeSandboxFactory),
    )
    backend = FCSandboxBackend(
        FCSandboxConfig(api_key="test-key", api_url="https://api.example.invalid", domain="invalid")
    )

    backend.run(valid_request())
    with pytest.raises(ExecutionError, match="FC_RUN_DUPLICATE"):
        backend.run(valid_request())


def test_fc_backend_rejects_persisted_duplicate_after_restart(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    create_calls: list[str] = []

    class PersistedSandboxFactory:
        @classmethod
        def create(cls, **kwargs: object) -> object:
            create_calls.append("create")
            return SimpleNamespace(
                sandbox_id="persisted-sandbox",
                files=SimpleNamespace(make_dir=lambda _path: None, write=lambda _path, _data: None),
                commands=SimpleNamespace(
                    run=lambda _command, timeout: SimpleNamespace(
                        exit_code=0, stdout="ok", stderr=""
                    )
                ),
                kill=lambda: None,
            )

    monkeypatch.setitem(
        sys.modules,
        "e2b_code_interpreter",
        SimpleNamespace(Sandbox=PersistedSandboxFactory),
    )
    config = FCSandboxConfig(
        api_key="test-key",
        api_url="https://api.example.invalid",
        domain="invalid",
        state_dir=str(tmp_path),
    )

    FCSandboxBackend(config).run(valid_request())
    restarted_backend = FCSandboxBackend(config)

    with pytest.raises(ExecutionError, match="FC_RUN_DUPLICATE"):
        restarted_backend.run(valid_request())
    assert create_calls == ["create"]


def test_fc_state_persistence_removes_temporary_file_on_write_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    backend = FCSandboxBackend(
        FCSandboxConfig(
            api_key="test-key",
            api_url="https://api.example.invalid",
            domain="invalid",
            state_dir=str(tmp_path),
        )
    )

    def fail_dump(*_args: object, **_kwargs: object) -> None:
        raise OSError("snapshot write failed")

    monkeypatch.setattr("devpilot.fc_sandbox.json.dump", fail_dump)

    with pytest.raises(OSError, match="snapshot write failed"):
        backend._reserve_run("write-failure")  # type: ignore[attr-defined]

    assert list(tmp_path.glob(".fc-run-*")) == []


def test_fc_recovery_does_not_resurrect_cancelled_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    connect_calls: list[str] = []

    class UnexpectedRecoveryFactory:
        @classmethod
        def connect(cls, sandbox_id: str, **kwargs: object) -> object:
            connect_calls.append(sandbox_id)
            raise AssertionError("cancelled runs must not reconnect")

    monkeypatch.setitem(
        sys.modules,
        "e2b_code_interpreter",
        SimpleNamespace(Sandbox=UnexpectedRecoveryFactory),
    )
    config = FCSandboxConfig(
        api_key="test-key",
        api_url="https://api.example.invalid",
        domain="invalid",
        state_dir=str(tmp_path),
    )
    backend = FCSandboxBackend(config)
    state = backend._reserve_run("cancelled-recovery")  # type: ignore[attr-defined]
    state.sandbox_id = "cancelled-sandbox"
    state.status = "cancelled"
    backend._persist_state(state)  # type: ignore[attr-defined]

    recovered = FCSandboxBackend(config).recover("cancelled-recovery")

    assert recovered["status"] == "cancelled"
    assert connect_calls == []


def test_fc_cleanup_retries_cancelled_run_after_restart(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    connect_calls: list[str] = []
    kill_calls: list[str] = []

    class RecoveredSandbox:
        def is_running(self) -> bool:
            return False

        def kill(self) -> None:
            kill_calls.append("kill")

    class RecoveryFactory:
        @classmethod
        def connect(cls, sandbox_id: str, **kwargs: object) -> RecoveredSandbox:
            connect_calls.append(sandbox_id)
            return RecoveredSandbox()

    monkeypatch.setitem(
        sys.modules,
        "e2b_code_interpreter",
        SimpleNamespace(Sandbox=RecoveryFactory),
    )
    config = FCSandboxConfig(
        api_key="test-key",
        api_url="https://api.example.invalid",
        domain="invalid",
        state_dir=str(tmp_path),
    )
    backend = FCSandboxBackend(config)
    state = backend._reserve_run("cleanup-after-restart")  # type: ignore[attr-defined]
    state.sandbox_id = "cleanup-sandbox"
    state.status = "cancelled"
    state.cleanup_error = "FC_CLEANUP_FAILED"
    state.cleanup_attempts = 1
    backend._persist_state(state)  # type: ignore[attr-defined]

    cleaned = FCSandboxBackend(config).cleanup("cleanup-after-restart")

    assert cleaned["status"] == "cancelled"
    assert cleaned["cleanup_status"] == "cleaned"
    assert connect_calls == ["cleanup-sandbox"]
    assert kill_calls == ["kill"]


def test_fc_stale_writer_cannot_overwrite_newer_recovery_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class RecoveredSandbox:
        def is_running(self) -> bool:
            return True

    class RecoveryFactory:
        @classmethod
        def connect(cls, _sandbox_id: str, **_kwargs: object) -> RecoveredSandbox:
            return RecoveredSandbox()

    monkeypatch.setitem(
        sys.modules,
        "e2b_code_interpreter",
        SimpleNamespace(Sandbox=RecoveryFactory),
    )
    config = FCSandboxConfig(
        api_key="test-key",
        api_url="https://api.example.invalid",
        domain="invalid",
        state_dir=str(tmp_path),
    )
    stale_backend = FCSandboxBackend(config)
    stale_state = stale_backend._reserve_run("stale-writer")  # type: ignore[attr-defined]
    stale_state.sandbox_id = "stale-sandbox"
    stale_state.status = "running"
    stale_backend._persist_state(stale_state)  # type: ignore[attr-defined]

    current_backend = FCSandboxBackend(config)
    assert current_backend.recover("stale-writer")["status"] == "running"

    stale_state.status = "completed"
    stale_backend._persist_state(stale_state)  # type: ignore[attr-defined]

    assert FCSandboxBackend(config).get_run_state("stale-writer")["status"] == "running"


def test_fc_backend_rejects_network_before_provider_call() -> None:
    backend = FCSandboxBackend(
        FCSandboxConfig(
            api_key="test-key",
            api_url="https://api.example.invalid",
            domain="example.invalid",
        )
    )
    with pytest.raises(ExecutionError, match="FC_NETWORK_DENIED"):
        backend.run(valid_request(network_allowed=True))


def test_fc_provider_default_mode_omits_unsupported_network_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[dict[str, object]] = []

    class FakeFiles:
        def make_dir(self, path: str) -> None:
            return None

        def write(self, path: str, content: bytes) -> None:
            return None

    class FakeCommands:
        def run(self, command: str, *, timeout: int) -> SimpleNamespace:
            return SimpleNamespace(exit_code=0, stdout="ok", stderr="")

    class FakeSandbox:
        sandbox_id = "degraded-sandbox"
        files = FakeFiles()
        commands = FakeCommands()

        def kill(self) -> None:
            return None

    class ProviderDefaultFactory:
        @classmethod
        def create(cls, **kwargs: object) -> FakeSandbox:
            events.append(kwargs)
            return FakeSandbox()

    monkeypatch.setitem(
        sys.modules,
        "e2b_code_interpreter",
        SimpleNamespace(Sandbox=ProviderDefaultFactory),
    )
    backend = FCSandboxBackend(
        FCSandboxConfig(
            api_key="test-key",
            api_url="https://api.example.invalid",
            domain="example.invalid",
            network_policy_mode="provider-default",
        )
    )

    result = backend.run(valid_request())

    assert backend.verification_available is True
    assert result.exit_code == 0
    assert result.side_effect_count == 0
    assert result.permission_violation is False
    assert "allow_internet_access" not in events[0]


def test_fc_backend_recovers_active_run_after_backend_restart(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    started = Event()
    released = Event()
    connected: list[str] = []

    class BlockingCommands:
        def run(self, command: str, *, timeout: int) -> SimpleNamespace:
            started.set()
            assert released.wait(2)
            return SimpleNamespace(
                exit_code=0, stdout="recovered E2B_API_KEY=secret-value", stderr=""
            )

    class SandboxHandle:
        def __init__(self, sandbox_id: str) -> None:
            self.sandbox_id = sandbox_id
            self.files = SimpleNamespace(
                make_dir=lambda _path: None, write=lambda _path, _data: None
            )
            self.commands = BlockingCommands()

        def is_running(self) -> bool:
            return not released.is_set()

        def kill(self) -> None:
            released.set()

    class RestartableFactory:
        @classmethod
        def create(cls, **kwargs: object) -> SandboxHandle:
            return SandboxHandle("restartable-sandbox")

        @classmethod
        def connect(cls, sandbox_id: str, **kwargs: object) -> SandboxHandle:
            connected.append(sandbox_id)
            return SandboxHandle(sandbox_id)

    monkeypatch.setitem(
        sys.modules,
        "e2b_code_interpreter",
        SimpleNamespace(Sandbox=RestartableFactory),
    )
    config = FCSandboxConfig(
        api_key="test-key",
        api_url="https://api.example.invalid",
        domain="example.invalid",
        state_dir=str(tmp_path),
    )
    first_backend = FCSandboxBackend(config)
    outcome: list[ExecutionResult] = []
    thread = Thread(target=lambda: outcome.append(first_backend.run(valid_request())))
    thread.start()
    assert started.wait(1)

    restarted_backend = FCSandboxBackend(config)
    state = restarted_backend.recover("run-1")

    assert state["status"] == "running"
    assert state["sandbox_id"] == "restartable-sandbox"
    assert connected == ["restartable-sandbox"]
    restarted_backend.cancel("run-1")
    released.set()
    thread.join(2)
    assert not thread.is_alive()
    assert outcome and outcome[0].exit_code == 0

    terminal_backend = FCSandboxBackend(config)
    terminal = terminal_backend.recover("run-1")
    assert terminal["status"] == "cancelled"
    assert terminal["cleanup_status"] == "cleaned"
    assert "result" not in terminal
    assert connected == ["restartable-sandbox"]


def test_fc_config_reads_explicit_project_allowlist(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("E2B_API_KEY", "test-key")
    monkeypatch.setenv("E2B_API_URL", "https://api.example.invalid")
    monkeypatch.setenv("E2B_DOMAIN", "cn-hangzhou.e2b.fc.aliyuncs.com")
    monkeypatch.setenv("DEVPILOT_FC_ALLOWED_PROJECTS", "E:/fixtures/a;E:/fixtures/b")

    config = FCSandboxConfig.from_environment()

    assert config.allowed_project_paths == ("E:\\fixtures\\a", "E:\\fixtures\\b")


def test_fc_project_allowlist_accepts_projects_below_a_configured_root() -> None:
    backend = FCSandboxBackend(
        FCSandboxConfig(
            api_key="test-key",
            api_url="https://api.example.invalid",
            domain="example.invalid",
            allowed_project_paths=("E:\\codex-local",),
        )
    )

    assert backend.project_allowed("E:\\codex-local\\training-projects\\demo")
    assert not backend.project_allowed("E:\\codex-local-other\\demo")


def test_fc_config_enables_pytest_bootstrap_by_default_and_allows_opt_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("E2B_API_KEY", "test-key")
    monkeypatch.setenv("E2B_API_URL", "https://api.example.invalid")
    monkeypatch.setenv("E2B_DOMAIN", "example.invalid")
    monkeypatch.delenv("DEVPILOT_FC_INSTALL_PYTEST", raising=False)

    assert FCSandboxConfig.from_environment().install_pytest is True

    monkeypatch.setenv("DEVPILOT_FC_INSTALL_PYTEST", "0")
    assert FCSandboxConfig.from_environment().install_pytest is False

    monkeypatch.setenv("DEVPILOT_FC_INSTALL_PYTEST", "maybe")
    with pytest.raises(ExecutionError, match="FC_CONFIG_INVALID"):
        FCSandboxConfig.from_environment()


def test_fc_backend_requires_network_permission_for_pytest_bootstrap() -> None:
    backend = FCSandboxBackend(
        FCSandboxConfig(
            api_key="test-key",
            api_url="https://api.example.invalid",
            domain="example.invalid",
        )
    )

    with pytest.raises(ExecutionError, match="FC_PYTEST_INSTALL_NETWORK_DENIED"):
        backend.run(valid_request(command_profile="project-pytest", command="python -m pytest -q"))


def test_fc_backend_exposes_only_real_evidenced_profiles() -> None:
    backend = FCSandboxBackend(
        FCSandboxConfig(
            api_key="test-key",
            api_url="https://api.example.invalid",
            domain="example.invalid",
            network_policy_mode="provider-default",
        )
    )

    assert backend.supports_profile("python-syntax")
    assert backend.supports_profile("python-run")
    assert backend.supports_profile("node-syntax")
    assert backend.supports_profile("python-regression")
    assert not backend.supports_profile("react-static")


def test_fc_backend_bootstraps_pytest_for_python_regression_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[tuple[object, ...]] = []

    class FakeCommands:
        def run(self, command: str, *, timeout: int, cwd: str | None = None) -> SimpleNamespace:
            events.append((command, timeout, cwd))
            return SimpleNamespace(exit_code=0, stdout="", stderr="")

    class FakeSandbox:
        sandbox_id = "python-regression-sandbox"
        files = SimpleNamespace(make_dir=lambda _path: None, write=lambda _path, _data: None)
        commands = FakeCommands()

        def kill(self) -> None:
            return None

    class Factory:
        @classmethod
        def create(cls, **_kwargs: object) -> FakeSandbox:
            return FakeSandbox()

    monkeypatch.setitem(sys.modules, "e2b_code_interpreter", SimpleNamespace(Sandbox=Factory))

    result = FCSandboxBackend(
        FCSandboxConfig(
            api_key="test-key",
            api_url="https://api.example.invalid",
            domain="example.invalid",
            network_policy_mode="provider-default",
        )
    ).run(valid_request(command_profile="python-regression", command="python -m pytest -q"))

    assert result.exit_code == 0
    assert events == [
        ("python -m pip install --disable-pip-version-check pytest", 120, None),
        ("python -m pytest -q", 300, "/workspace/devpilot"),
    ]


def test_fc_backend_cancel_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    started = Event()
    killed = Event()
    outcome: list[ExecutionError] = []

    class FakeCommands:
        def run(self, command: str, *, timeout: int) -> SimpleNamespace:
            started.set()
            killed.wait(2)
            return SimpleNamespace(exit_code=0, stdout="late", stderr="")

    class FakeSandbox:
        sandbox_id = "cancel-sandbox"
        files = SimpleNamespace(make_dir=lambda _path: None, write=lambda _path, _data: None)
        commands = FakeCommands()

        def kill(self) -> None:
            killed.set()

    class CancelFactory:
        @classmethod
        def create(cls, **kwargs: object) -> FakeSandbox:
            return FakeSandbox()

    monkeypatch.setitem(
        sys.modules,
        "e2b_code_interpreter",
        SimpleNamespace(Sandbox=CancelFactory),
    )
    backend = FCSandboxBackend(
        FCSandboxConfig(api_key="test-key", api_url="https://api.example.invalid", domain="invalid")
    )

    def execute() -> None:
        try:
            backend.run(valid_request())
        except ExecutionError as exc:
            outcome.append(exc)

    thread = Thread(target=execute, daemon=True)
    thread.start()
    assert started.wait(1)
    backend.cancel("run-1")
    backend.cancel("run-1")
    thread.join(2)

    assert not thread.is_alive()
    assert outcome and outcome[0].code == "FC_CANCELLED"
    assert backend.get_run_state("run-1")["status"] == "cancelled"


def test_fc_backend_preserves_sdk_nonzero_exit_result(monkeypatch: pytest.MonkeyPatch) -> None:
    class NonZeroCommandError(Exception):
        exit_code = 2
        stdout = ""
        stderr = "syntax error"

    class NonZeroSandbox:
        sandbox_id = "nonzero-sandbox"
        files = SimpleNamespace(make_dir=lambda _path: None, write=lambda _path, _data: None)
        commands = SimpleNamespace(
            run=lambda _command, timeout: (_ for _ in ()).throw(NonZeroCommandError())
        )

        def kill(self) -> None:
            return None

    class NonZeroFactory:
        @classmethod
        def create(cls, **kwargs: object) -> NonZeroSandbox:
            return NonZeroSandbox()

    monkeypatch.setitem(
        sys.modules,
        "e2b_code_interpreter",
        SimpleNamespace(Sandbox=NonZeroFactory),
    )
    backend = FCSandboxBackend(
        FCSandboxConfig(api_key="test-key", api_url="https://api.example.invalid", domain="invalid")
    )

    result = backend.run(valid_request())

    assert result.exit_code == 2
    assert result.stderr == "syntax error"
    assert backend.get_run_state("run-1")["status"] == "failed"


def test_fc_backend_surfaces_cleanup_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    class CleanupFailureSandbox:
        sandbox_id = "cleanup-failure-sandbox"
        files = SimpleNamespace(make_dir=lambda _path: None, write=lambda _path, _data: None)
        commands = SimpleNamespace(
            run=lambda _command, timeout: SimpleNamespace(exit_code=0, stdout="ok", stderr="")
        )

        def kill(self) -> None:
            raise RuntimeError("cleanup unavailable")

    class CleanupFailureFactory:
        @classmethod
        def create(cls, **kwargs: object) -> CleanupFailureSandbox:
            return CleanupFailureSandbox()

    monkeypatch.setitem(
        sys.modules,
        "e2b_code_interpreter",
        SimpleNamespace(Sandbox=CleanupFailureFactory),
    )
    backend = FCSandboxBackend(
        FCSandboxConfig(api_key="test-key", api_url="https://api.example.invalid", domain="invalid")
    )

    with pytest.raises(ExecutionError, match="FC_CLEANUP_FAILED"):
        backend.run(valid_request())

    state = backend.get_run_state("run-1")
    assert state["status"] == "cleanup_failed"
    assert state["cleanup_error"] == "FC_CLEANUP_FAILED"
    assert state["sandbox_id"] == "cleanup-failure-sandbox"
    assert state["cleanup_status"] == "pending"
    backend.cleanup("run-1")
    backend.cleanup("run-1")
    assert backend.get_run_state("run-1")["cleanup_status"] == "manual_pending"


def test_fc_backend_recovery_marks_unqueryable_sandbox_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class RecoveryFactory:
        @classmethod
        def connect(cls, sandbox_id: str, **kwargs: object) -> object:
            raise RuntimeError("provider query failed")

    monkeypatch.setitem(
        sys.modules,
        "e2b_code_interpreter",
        SimpleNamespace(Sandbox=RecoveryFactory),
    )
    backend = FCSandboxBackend(
        FCSandboxConfig(api_key="test-key", api_url="https://api.example.invalid", domain="invalid")
    )

    state = backend.recover("run-after-restart", "remote-sandbox")

    assert state["status"] == "unknown"
    assert state["sandbox_id"] == "remote-sandbox"
    assert state["error_code"] == "FC_RECOVERY_UNKNOWN"


def test_fc_recovered_run_cancel_converges_to_cancelled(monkeypatch: pytest.MonkeyPatch) -> None:
    class RecoveredSandbox:
        def is_running(self) -> bool:
            return True

        def kill(self) -> None:
            return None

    class RecoveryFactory:
        @classmethod
        def connect(cls, _sandbox_id: str, **_kwargs: object) -> RecoveredSandbox:
            return RecoveredSandbox()

    monkeypatch.setitem(
        sys.modules,
        "e2b_code_interpreter",
        SimpleNamespace(Sandbox=RecoveryFactory),
    )
    backend = FCSandboxBackend(
        FCSandboxConfig(api_key="test-key", api_url="https://api.example.invalid", domain="invalid")
    )

    backend.recover("run-after-restart", "remote-sandbox")
    state = backend.cancel("run-after-restart")

    assert state["status"] == "cancelled"


def test_fc_cancel_kill_failure_still_records_cancelled(monkeypatch: pytest.MonkeyPatch) -> None:
    class UnavailableSandbox:
        def kill(self) -> None:
            raise RuntimeError("kill unavailable")

    backend = FCSandboxBackend(
        FCSandboxConfig(api_key="test-key", api_url="https://api.example.invalid", domain="invalid")
    )
    state = backend._reserve_run("run-cancel-failure")  # type: ignore[attr-defined]
    state.handle = UnavailableSandbox()
    state.sandbox_id = "cancel-failure-sandbox"
    state.status = "running"

    result = backend.cancel("run-cancel-failure")

    assert result["status"] == "cancelled"
    assert result["cleanup_error"] == "FC_CLEANUP_FAILED"


def test_fc_backend_classifies_unsupported_provider_capability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class UnsupportedSandboxFactory:
        @classmethod
        def create(cls, **kwargs: object) -> object:
            raise RuntimeError("400: Feature 'SessionNetworkPolicy' is not supported")

    monkeypatch.setitem(
        sys.modules,
        "e2b_code_interpreter",
        SimpleNamespace(Sandbox=UnsupportedSandboxFactory),
    )
    backend = FCSandboxBackend(
        FCSandboxConfig(
            api_key="test-key",
            api_url="https://api.example.invalid",
            domain="example.invalid",
        )
    )

    with pytest.raises(ExecutionError, match="FC_CAPABILITY_UNSUPPORTED"):
        backend.run(valid_request())
