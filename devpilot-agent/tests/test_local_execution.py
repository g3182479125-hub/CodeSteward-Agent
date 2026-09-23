from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import replace
from pathlib import Path

import pytest

from devpilot.backend_factory import build_project_execution_backend
from devpilot.control_plane import ControlPlaneManager, _backend_catalog, create_app
from devpilot.execution import ExecutionError, ExecutionFile, ExecutionRequest, ExecutionResult
from devpilot.fc_sandbox import build_fc_backend_from_environment
from devpilot.local_execution import LocalExecutionBackend
from devpilot.paths import AllowedRoots
from devpilot.storage import ProjectStore
from devpilot.test_runs import ProjectTestRunner


def _request(tmp_path: Path, source: str) -> ExecutionRequest:
    return ExecutionRequest(
        run_id="local-test-run",
        project_id="local-test-project",
        files=(
            ExecutionFile("tests/__init__.py", b""),
            ExecutionFile("tests/test_sample.py", source.encode("utf-8")),
        ),
        command="python -m unittest discover -v",
        timeout_seconds=15,
        command_profile="project-unittest",
        working_directory="",
    )


def test_local_backend_runs_unittest_snapshot_and_returns_local_receipt(
    tmp_path: Path,
) -> None:
    backend = LocalExecutionBackend(state_dir=tmp_path / "local-runs")
    request = _request(
        tmp_path,
        "import unittest\n\n"
        "class SampleTest(unittest.TestCase):\n"
        "    def test_passes(self):\n"
        "        self.assertEqual(2 + 2, 4)\n",
    )

    prepared = backend.prepare(request, dependency_fingerprint="fixture-v1")
    receipt = backend.run(request)

    assert prepared["status"] == "ready"
    assert receipt.backend == "local"
    assert receipt.sandbox_id == ""
    assert receipt.exit_code == 0
    assert "Ran 1 test" in receipt.stderr
    assert receipt.failure_kind == "semantic"
    assert backend.get_run_state(request.run_id)["cleanup_status"] == "cleaned"


def test_local_backend_returns_real_nonzero_test_exit_code(tmp_path: Path) -> None:
    backend = LocalExecutionBackend(state_dir=tmp_path / "local-runs")
    request = _request(
        tmp_path,
        "import unittest\n\n"
        "class SampleTest(unittest.TestCase):\n"
        "    def test_fails(self):\n"
        "        self.assertEqual(2 + 2, 5)\n",
    )

    backend.prepare(request, dependency_fingerprint="fixture-v1")
    receipt = backend.run(request)

    assert receipt.exit_code != 0
    assert "FAILED" in receipt.stderr
    assert backend.get_run_state(request.run_id)["cleanup_status"] == "cleaned"


def test_local_backend_enforces_timeout_and_cleans_workspace(tmp_path: Path) -> None:
    backend = LocalExecutionBackend(state_dir=tmp_path / "local-runs")
    request = replace(
        _request(
            tmp_path,
            "import time\n"
            "import unittest\n\n"
            "class SlowTest(unittest.TestCase):\n"
            "    def test_waits(self):\n"
            "        time.sleep(10)\n",
        ),
        timeout_seconds=1,
    )

    backend.prepare(request)
    with pytest.raises(ExecutionError, match="LOCAL_TIMEOUT"):
        backend.run(request)

    assert backend.get_run_state(request.run_id)["cleanup_status"] == "cleaned"
    assert list((tmp_path / "local-runs").iterdir()) == []


def test_local_backend_does_not_claim_cleanup_when_workspace_removal_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = LocalExecutionBackend(state_dir=tmp_path / "local-runs")
    request = _request(
        tmp_path,
        "import unittest\n\n"
        "class SampleTest(unittest.TestCase):\n"
        "    def test_passes(self):\n"
        "        self.assertTrue(True)\n",
    )
    backend.prepare(request)
    temporary_root = next(iter(backend._runs.values())).root
    original_cleanup = tempfile.TemporaryDirectory.cleanup

    def reject_cleanup(_temporary: tempfile.TemporaryDirectory[str]) -> None:
        raise PermissionError("fixture cleanup failure")

    monkeypatch.setattr(tempfile.TemporaryDirectory, "cleanup", reject_cleanup)
    with pytest.raises(ExecutionError, match="LOCAL_CLEANUP_FAILED"):
        backend.run(request)

    run_state = backend.get_run_state(request.run_id)
    assert run_state["cleanup_status"] == "cleanup_failed"
    assert run_state["status"] == "cleanup_failed"

    monkeypatch.setattr(tempfile.TemporaryDirectory, "cleanup", original_cleanup)
    shutil.rmtree(temporary_root, ignore_errors=True)


def test_local_backend_records_prepare_cleanup_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = LocalExecutionBackend(state_dir=tmp_path / "local-runs")
    request = _request(tmp_path, "pass")
    original_cleanup = tempfile.TemporaryDirectory.cleanup

    def fail_prepare(_run: object) -> None:
        raise ExecutionError("LOCAL_DEPENDENCY_INSTALL_FAILED", "fixture failure")

    def reject_cleanup(_temporary: tempfile.TemporaryDirectory[str]) -> None:
        raise PermissionError("fixture cleanup failure")

    monkeypatch.setattr(backend, "_prepare_dependencies", fail_prepare)
    monkeypatch.setattr(tempfile.TemporaryDirectory, "cleanup", reject_cleanup)
    with pytest.raises(ExecutionError) as error:
        backend.prepare(request)

    assert error.value.details["cleanup_status"] == "cleanup_failed"
    run_state = backend.get_run_state(request.run_id)
    assert run_state["cleanup_status"] == "cleanup_failed"
    assert run_state["status"] == "cleanup_failed"

    monkeypatch.setattr(tempfile.TemporaryDirectory, "cleanup", original_cleanup)
    for temporary_root in (tmp_path / "local-runs").iterdir():
        shutil.rmtree(temporary_root, ignore_errors=True)


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object behavior")
def test_local_process_runner_stops_descendants_after_parent_exits(tmp_path: Path) -> None:
    child_code = "import time; time.sleep(30)"
    parent_code = (
        "import subprocess, sys; "
        "child = subprocess.Popen([sys.executable, '-c', "
        f"{child_code!r}], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL); "
        "print(child.pid, flush=True)"
    )

    completed = LocalExecutionBackend._run_process(
        [sys.executable, "-c", parent_code],
        cwd=tmp_path,
        env=os.environ.copy(),
        timeout=10,
        max_capture_bytes=1_000,
    )

    child_pid = completed.stdout.decode("utf-8").strip()
    tasklist = subprocess.run(
        ["tasklist", "/FI", f"PID eq {child_pid}", "/FO", "CSV"],
        capture_output=True,
        text=True,
        check=True,
        shell=False,
    )
    assert child_pid not in tasklist.stdout


def test_windows_spawn_cleanup_timeout_is_reported_as_cleanup_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import io

    from devpilot import local_execution

    class StubbornProcess:
        pid = 99
        stdout = io.BytesIO()
        stderr = io.BytesIO()
        returncode = None

        def kill(self) -> None:
            return None

        def wait(self, timeout: int | None = None) -> int:
            raise subprocess.TimeoutExpired("fixture", timeout)

    class BrokenJob:
        def assign(self, _process: subprocess.Popen[bytes]) -> None:
            raise OSError("fixture assignment failure")

        def terminate_and_wait(self) -> bool:
            return True

        def close(self) -> bool:
            return True

    monkeypatch.setattr(local_execution, "_create_windows_process_job", BrokenJob)
    monkeypatch.setattr(local_execution, "_resume_suspended_windows_process", lambda _pid: None)
    monkeypatch.setattr(
        local_execution.subprocess,
        "Popen",
        lambda *_args, **_kwargs: StubbornProcess(),
    )
    monkeypatch.setattr(
        local_execution.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            args="taskkill", returncode=1, stdout=b"", stderr=b""
        ),
    )

    with pytest.raises(ExecutionError, match="LOCAL_PROCESS_CLEANUP_FAILED") as error:
        LocalExecutionBackend._run_process(
            ["python", "-c", "pass"],
            cwd=tmp_path,
            env={},
            timeout=1,
            max_capture_bytes=1_000,
        )

    assert error.value.details["cleanup_status"] == "cleanup_failed"
    assert error.value.details["safe_to_retry"] is False


@pytest.mark.parametrize(
    ("source", "expected_exit_code"),
    [
        ("def test_value():\n    assert 3 * 3 == 9\n", 0),
        ("def test_value():\n    assert 3 * 3 == 8\n", 1),
    ],
)
def test_local_backend_runs_pytest_profiles(
    tmp_path: Path, source: str, expected_exit_code: int
) -> None:
    backend = LocalExecutionBackend(state_dir=tmp_path / "local-runs")
    request = replace(
        _request(tmp_path, source),
        files=(ExecutionFile("test_sample.py", source.encode("utf-8")),),
        command="python -m pytest -q",
        command_profile="project-pytest",
    )

    backend.prepare(request)
    receipt = backend.run(request)

    assert receipt.exit_code == expected_exit_code
    assert receipt.execution_location == "local"
    assert receipt.network_policy == "host-default-unrestricted"


@pytest.mark.parametrize(
    ("source", "expected_exit_code"),
    [("process.exitCode = 0;\n", 0), ("process.exitCode = 7;\n", 7)],
)
def test_local_backend_runs_npm_test_profiles(
    tmp_path: Path, source: str, expected_exit_code: int
) -> None:
    backend = LocalExecutionBackend(state_dir=tmp_path / "local-runs")
    files = (
        ExecutionFile(
            "package.json",
            b'{"name":"local-fixture","version":"1.0.0","scripts":{"test":"node test.js"}}',
        ),
        ExecutionFile(
            "package-lock.json",
            b'{"name":"local-fixture","version":"1.0.0","lockfileVersion":3,"requires":true,"packages":{"":{"name":"local-fixture","version":"1.0.0"}}}',
        ),
        ExecutionFile("test.js", source.encode("utf-8")),
    )
    request = replace(
        _request(tmp_path, "pass"),
        files=files,
        command="npm test",
        command_profile="project-npm-test",
        dependency_profile="node-npm-ci",
    )

    backend.prepare(request)
    receipt = backend.run(request)

    assert receipt.exit_code == expected_exit_code
    assert receipt.execution_location == "local"


def test_local_backend_rejects_non_allowlisted_command_before_materializing(
    tmp_path: Path,
) -> None:
    backend = LocalExecutionBackend(state_dir=tmp_path / "local-runs")
    request = replace(_request(tmp_path, "pass"), command="python -c 'print(1)'")

    with pytest.raises(ExecutionError, match="allowlisted"):
        backend.prepare(request, dependency_fingerprint="fixture-v1")

    assert not (tmp_path / "local-runs").exists()


def test_local_backend_rejects_command_that_does_not_match_profile(
    tmp_path: Path,
) -> None:
    backend = LocalExecutionBackend(state_dir=tmp_path / "local-runs")
    request = replace(_request(tmp_path, "pass"), command="npm test")

    with pytest.raises(ExecutionError, match="does not match"):
        backend.prepare(request, dependency_fingerprint="fixture-v1")


def test_local_backend_bounds_test_output(tmp_path: Path) -> None:
    backend = LocalExecutionBackend(state_dir=tmp_path / "local-runs")
    request = replace(
        _request(
            tmp_path,
            "import sys\n"
            "import unittest\n\n"
            "class SampleTest(unittest.TestCase):\n"
            "    def test_output(self):\n"
            "        sys.stdout.write('x' * 20000)\n",
        ),
        max_output_bytes=1_000,
    )

    backend.prepare(request, dependency_fingerprint="fixture-v1")
    receipt = backend.run(request)

    assert receipt.exit_code == 0
    assert receipt.output_truncated is True
    assert len(receipt.stdout.encode("utf-8")) <= request.max_output_bytes


def test_project_execution_backend_defaults_to_local_without_fc_settings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DEVPILOT_ENABLE_FC_SANDBOX", raising=False)
    monkeypatch.delenv("E2B_API_KEY", raising=False)
    monkeypatch.delenv("E2B_API_URL", raising=False)
    monkeypatch.delenv("E2B_DOMAIN", raising=False)

    backend = build_project_execution_backend(state_dir=tmp_path / "state")

    assert isinstance(backend, LocalExecutionBackend)

    store = ProjectStore(tmp_path / "state" / "devpilot.db")
    store.initialize()
    manager = ControlPlaneManager(
        allowed_roots=AllowedRoots.from_paths([tmp_path]),
        store=store,
        execution_backend=backend,
    )
    catalog = _backend_catalog(manager)[0]
    assert catalog["execution_location"] == "local"
    assert catalog["network_allowed"] is True
    assert catalog["network_policy"] == "host-default-unrestricted"
    assert catalog["os_isolation"] is False


def test_project_execution_backend_requires_exact_fc_opt_in(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEVPILOT_ENABLE_FC_SANDBOX", "true")

    with pytest.raises(ExecutionError, match="DEVPILOT_ENABLE_FC_SANDBOX"):
        build_project_execution_backend(state_dir=tmp_path / "state")


def test_direct_fc_backend_construction_is_disabled_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DEVPILOT_ENABLE_FC_SANDBOX", raising=False)

    with pytest.raises(ExecutionError, match="FC_SANDBOX_DISABLED"):
        build_fc_backend_from_environment()


def test_fc_backend_factory_requires_explicit_opt_in(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from devpilot import fc_sandbox

    sentinel = object()
    monkeypatch.setenv("DEVPILOT_ENABLE_FC_SANDBOX", "1")
    monkeypatch.setattr(fc_sandbox, "build_fc_backend_from_environment", lambda: sentinel)

    assert build_project_execution_backend(state_dir=tmp_path / "state") is sentinel


def test_control_plane_uses_local_backend_without_fc_credentials(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DEVPILOT_ENABLE_FC_SANDBOX", raising=False)
    monkeypatch.delenv("DEVPILOT_ENABLE_MODEL_CANDIDATES", raising=False)
    monkeypatch.delenv("E2B_API_KEY", raising=False)
    monkeypatch.delenv("E2B_API_URL", raising=False)
    monkeypatch.delenv("E2B_DOMAIN", raising=False)
    monkeypatch.setattr(ProjectStore, "require_schema_head", lambda *_args: None)
    ProjectStore(tmp_path / "state" / "devpilot.db").initialize()

    app = create_app(
        data_dir=tmp_path / "state",
        allowed_roots=AllowedRoots.from_paths([tmp_path]),
        internal_auth_token="local-test-control-plane-token-1234567890",
    )

    assert app.state.manager.execution_backend.name == "local"


@pytest.mark.parametrize(
    "receipt",
    [
        ExecutionResult(
            backend="fc-agent-sandbox",
            sandbox_id="sandbox-1",
            exit_code=0,
            stdout="",
            stderr="",
            duration_ms=1,
            command_profile="project-unittest",
            side_effect_count=0,
            permission_violation=False,
            execution_location="unknown",
        ),
        ExecutionResult(
            backend="fc-agent-sandbox",
            sandbox_id="sandbox-1",
            exit_code=0,
            stdout="",
            stderr="",
            duration_ms=1,
            command_profile="project-unittest",
            side_effect_count=0,
            permission_violation=False,
            execution_location="local",
        ),
    ],
)
def test_project_test_runner_rejects_unknown_or_inconsistent_receipt_locations(
    receipt: ExecutionResult,
) -> None:
    assert not ProjectTestRunner._receipt_is_complete(receipt, "project-unittest")


def test_project_test_run_uses_local_unittest_and_marks_receipt_location(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    tests = project / "tests"
    tests.mkdir(parents=True)
    (project / "main.py").write_text("value = 4\n", encoding="utf-8")
    (tests / "__init__.py").write_text("", encoding="utf-8")
    (tests / "test_main.py").write_text(
        "import unittest\n"
        "from main import value\n\n"
        "class ValueTest(unittest.TestCase):\n"
        "    def test_value(self):\n"
        "        self.assertEqual(value, 4)\n",
        encoding="utf-8",
    )
    store = ProjectStore(tmp_path / "data" / "devpilot.db")
    store.initialize()
    backend = LocalExecutionBackend(state_dir=tmp_path / "local-runs")
    manager = ControlPlaneManager(
        allowed_roots=AllowedRoots.from_paths([tmp_path]),
        store=store,
        execution_backend=backend,
    )

    receipt = manager.run_project_test(
        project_path=project,
        test_kind="python-unittest",
        target_files=["main.py"],
        symptom="Check the standard-library test path.",
        expected_behavior="The unittest suite passes locally.",
        session_id="websocket:local-test",
        session_access_mode="full",
    )

    assert receipt["status"] == "passed"
    assert receipt["execution_location"] == "local"
    assert receipt["sandbox_id"] is None
    events = store.list_run_events(receipt["run_id"])
    execution = next(event for event in events if event["event_type"] == "test.execution.completed")
    assert execution["payload"]["execution_location"] == "local"
    assert execution["payload"]["sandbox_id"] is None


def test_project_test_run_persists_prepare_cleanup_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    tests = project / "tests"
    tests.mkdir(parents=True)
    (tests / "test_main.py").write_text("pass\n", encoding="utf-8")
    store = ProjectStore(tmp_path / "data" / "devpilot.db")
    store.initialize()
    backend = LocalExecutionBackend(state_dir=tmp_path / "local-runs")
    original_cleanup = tempfile.TemporaryDirectory.cleanup

    def fail_prepare(_run: object) -> None:
        raise ExecutionError("LOCAL_DEPENDENCY_INSTALL_FAILED", "fixture failure")

    def reject_cleanup(_temporary: tempfile.TemporaryDirectory[str]) -> None:
        raise PermissionError("fixture cleanup failure")

    monkeypatch.setattr(backend, "_prepare_dependencies", fail_prepare)
    monkeypatch.setattr(tempfile.TemporaryDirectory, "cleanup", reject_cleanup)
    manager = ControlPlaneManager(
        allowed_roots=AllowedRoots.from_paths([tmp_path]),
        store=store,
        execution_backend=backend,
    )

    result = manager.run_project_test(
        project_path=project,
        test_kind="python-unittest",
        target_files=["tests/test_main.py"],
        symptom="exercise failed local preparation cleanup",
        expected_behavior="cleanup failure remains visible in the Run",
        session_id="websocket:cleanup-failure",
        session_access_mode="full",
    )

    assert result["cleanup_status"] == "cleanup_failed"
    events = store.list_run_events(result["run_id"])
    failure = next(event for event in events if event["event_type"] == "test.prepare.failed")
    assert failure["payload"]["cleanup_status"] == "cleanup_failed"

    monkeypatch.setattr(
        tempfile.TemporaryDirectory,
        "cleanup",
        original_cleanup,
    )
    for temporary_root in (tmp_path / "local-runs").iterdir():
        shutil.rmtree(temporary_root, ignore_errors=True)
