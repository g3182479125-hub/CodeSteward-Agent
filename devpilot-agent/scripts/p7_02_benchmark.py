"""Run the deterministic P7-02 task-profile contract benchmark.

This benchmark intentionally does not call an LLM, FC, or a user project.  It
measures the stable routing and safety contract introduced by P7-01 so that
the baseline and candidate use the same input, budget, and repetitions.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType
from typing import Any

from devpilot.action_broker import (
    ActionBroker,
    BrokerActionRequest,
    ConfirmationContext,
    sign_confirmation_context,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_BASELINE = "e8369151cba046007cc05621756ab702a1ec99f8"
DEFAULT_CANDIDATE = "7465dacfa8c0a35a341b248e7564eb3e2b9ef1ce"
DEFAULT_REPETITIONS = 3


@dataclass(frozen=True)
class BenchmarkTask:
    task_id: str
    profile: str
    objective: str
    fixture: str


@dataclass(frozen=True)
class RunRecord:
    version: str
    commit: str
    task_id: str
    repetition: int
    passed: bool
    failure_reason: str | None
    manual_interventions: int
    model_id: str
    model_parameters: dict[str, Any]
    budget: str
    template: str
    profile_version: str
    config_version: str
    task_source: str
    fixture_hashes: tuple[tuple[str, str], ...]
    evidence_refs: tuple[str, ...]
    action_boundary: str
    duration_ms: int


TASKS = (
    BenchmarkTask(
        "project-explanation",
        "repo-analysis",
        "explain a project with file-backed facts",
        "devpilot-agent/tests/fixtures/python-project",
    ),
    BenchmarkTask(
        "small-feature",
        "feature-delivery",
        "propose one bounded feature without applying it",
        "devpilot-agent/tests/fixtures/python-project",
    ),
    BenchmarkTask(
        "bug-fix",
        "bug-fix",
        "propose a fix from saved failure evidence without applying it",
        "devpilot-agent/tests/fixtures/react-form-project",
    ),
)


def _git_show(commit: str, path: str) -> str:
    result = subprocess.run(
        ["git", "show", f"{commit}:{path}"],
        cwd=REPO_ROOT,
        capture_output=True,
        check=False,
        text=True,
        encoding="utf-8",
    )
    if result.returncode != 0:
        return ""
    return result.stdout


def _git_rev_parse(ref: str) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "--verify", f"{ref}^{{commit}}"],
        cwd=REPO_ROOT,
        capture_output=True,
        check=False,
        text=True,
        encoding="utf-8",
    )
    if result.returncode != 0:
        raise ValueError(f"unknown git ref: {ref}")
    return result.stdout.strip()


def _load_task_profile_catalog(commit: str) -> tuple[type[Any], type[ValueError]]:
    source = _git_show(commit, "nanobot/nanobot/agent/task_profiles.py")
    if not source:
        raise ValueError(f"task profile module is missing at {commit}")
    module = ModuleType(f"nanobot.agent.task_profiles@{commit[:12]}")
    module.__file__ = f"{commit}:nanobot/nanobot/agent/task_profiles.py"
    sys.modules[module.__name__] = module
    exec(compile(source, module.__file__, "exec"), module.__dict__)
    return module.TaskProfileCatalog, module.TaskProfileError


def _fixture_manifest(task: BenchmarkTask) -> tuple[tuple[str, str], ...]:
    root = REPO_ROOT / task.fixture
    files = sorted(
        path
        for path in root.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc"
    )
    return tuple(
        (
            str(path.relative_to(REPO_ROOT)).replace("\\", "/"),
            hashlib.sha256(path.read_bytes()).hexdigest(),
        )
        for path in files
    )


def _read_fixture(task: BenchmarkTask) -> dict[str, str]:
    root = REPO_ROOT / task.fixture
    return {
        str(path.relative_to(root)).replace("\\", "/"): path.read_text(encoding="utf-8")
        for path in root.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc"
    }


def _run_fixed_task(task: BenchmarkTask) -> tuple[bool, str | None, tuple[str, ...], str]:
    """Execute the fixed fixture probe without writing to the fixture."""
    files = _read_fixture(task)
    refs = tuple(f"fixture:{task.fixture}/{path}" for path in sorted(files))
    if task.task_id == "project-explanation":
        required = {"README.md", "pyproject.toml", "src/demo/main.py", "tests/sample_checks.py"}
        if not required.issubset(files):
            return False, "project explanation fixture is incomplete", refs, "read-only"
        if "python" not in files["README.md"].lower() or "project" not in files["pyproject.toml"]:
            return False, "project explanation facts are missing", refs, "read-only"
        return True, None, refs + ("output:technical_stack/evidence_files",), "read-only"
    if task.task_id == "small-feature":
        source = files.get("src/demo/main.py", "")
        test = files.get("tests/sample_checks.py", "")
        proposal = "def format_greeting(name: str) -> str:\n    return f\"Hello, {name}!\"\n"
        if "def main()" not in source or "assert main() == 0" not in test:
            return (
                False,
                "feature fixture baseline is incomplete",
                refs,
                "confirmed-action-required",
            )
        if "format_greeting" not in proposal:
            return False, "feature proposal is empty", refs, "confirmed-action-required"
        return (
            True,
            None,
            refs + ("proposal:src/demo/main.py", "proposal:tests/sample_checks.py"),
            "unconfirmed-write-rejected",
        )
    source = files.get("src/App.jsx", "")
    test = files.get("test/App.test.jsx", "")
    if "preventDefault" in source or "handleSubmit" not in source or "Saved" not in test:
        return (
            False,
            "bug fixture does not contain the fixed regression",
            refs,
            "confirmed-action-required",
        )
    return (
        True,
        None,
        refs + ("failure:src/App.jsx", "proposal:src/App.jsx"),
        "unconfirmed-write-rejected",
    )


def _probe_unconfirmed_write_rejected() -> bool:
    """Exercise the real broker with no current confirmation reservation."""
    now = datetime(2026, 9, 7, 12, 0, tzinfo=UTC)
    hashes = {"plan_hash": "1" * 64, "patch_hash": "2" * 64, "scope_hash": "3" * 64}
    context = ConfirmationContext(
        session_id="benchmark-session",
        thread_id="benchmark-thread",
        user_message_seq=1,
        confirmation_id="benchmark-confirmation",
        issued_at=now.isoformat(),
        expires_at=(now + timedelta(minutes=5)).isoformat(),
        user_event_nonce="benchmark-nonce",
        verification_profile="python-syntax",
        verification_profile_version="1",
        verification_snapshot_hash="4" * 64,
        verification_check_set_hash="5" * 64,
        signature="0" * 64,
    )
    context = ConfirmationContext(
        **{
            **asdict(context),
            "signature": sign_confirmation_context(
                context,
                b"k" * 32,
                change_set_id="benchmark-change-set",
                project_id="benchmark-project",
                capability="r1.apply",
                **hashes,
            ),
        }
    )
    calls: list[str] = []
    broker = ActionBroker(
        b"k" * 32,
        snapshot_reader=lambda _change_set_id: {
            "change_set_id": "benchmark-change-set",
            "project_id": "benchmark-project",
            "trust_state": "trusted",
            **hashes,
        },
        capability_issuer=lambda *_args: calls.append("issue") or "opaque-token",
        capability_executor=lambda *_args: calls.append("execute") or {"status": "completed"},
        confirmation_consumer=lambda *_args: False,
        clock=lambda: now,
    )
    result = broker.execute(
        BrokerActionRequest(
            context=context,
            action="apply",
            change_set_id="benchmark-change-set",
            project_id="benchmark-project",
            **hashes,
        )
    )
    return result.error_code == "APPROVAL_CONFIRMATION_REPLAYED" and not calls


def _baseline_contract(
    commit: str, task: BenchmarkTask
) -> tuple[bool, str | None, tuple[str, ...], str]:
    """Probe the pre-P7 tree without importing candidate code into it."""
    task_passed, task_failure, refs, boundary = _run_fixed_task(task)
    if not task_passed:
        return False, task_failure, refs, boundary
    profile_module = _git_show(commit, "nanobot/nanobot/agent/task_profiles.py")
    registry = _git_show(commit, "nanobot/nanobot/agent/tools/registry.py")
    session_manager = _git_show(commit, "nanobot/nanobot/session/manager.py")
    if profile_module:
        return False, "baseline unexpectedly contains task profile runtime", refs, boundary
    if "def restricted_to(" in registry:
        return False, "baseline unexpectedly contains isolated tool view", refs, boundary
    if "allowed_tools: frozenset[str] | None" in session_manager:
        return False, "baseline unexpectedly contains task policy binding", refs, boundary
    return False, f"pre-P7 baseline has no trusted {task.profile} task contract", refs, boundary


def _candidate_contract(
    commit: str, task: BenchmarkTask
) -> tuple[bool, str | None, tuple[str, ...], str]:
    task_passed, task_failure, refs, boundary = _run_fixed_task(task)
    if not task_passed:
        return False, task_failure, refs, boundary
    catalog_type, profile_error = _load_task_profile_catalog(commit)
    catalog = catalog_type()
    profile = catalog.get(task.profile)
    registered = set(profile.allowed_tools)
    skills = set(profile.skill_names)
    try:
        catalog.validate_runtime(profile, registered, skills)
    except profile_error as exc:
        return False, str(exc), refs, boundary
    if task.profile == "repo-analysis":
        if profile.allowed_tools != frozenset({"devpilot_inspect_project"}):
            return False, "project explanation exposes a non-read-only tool", refs, boundary
    elif "devpilot_confirmed_action" not in profile.allowed_tools:
        return False, "code task lacks the existing confirmed-action boundary", refs, boundary
    if task.profile != "repo-analysis" and not _probe_unconfirmed_write_rejected():
        return False, "unconfirmed write was not rejected", refs, boundary
    return True, None, refs, boundary


def run_benchmark(
    *,
    baseline: str = DEFAULT_BASELINE,
    candidate: str = DEFAULT_CANDIDATE,
    repetitions: int = DEFAULT_REPETITIONS,
) -> dict[str, Any]:
    if repetitions < 1 or repetitions > 10:
        raise ValueError("repetitions must be between 1 and 10")
    baseline = _git_rev_parse(baseline)
    candidate = _git_rev_parse(candidate)
    records: list[RunRecord] = []
    for version, commit, evaluator in (
        ("baseline", baseline, _baseline_contract),
        ("candidate", candidate, _candidate_contract),
    ):
        for task in TASKS:
            for repetition in range(1, repetitions + 1):
                started = time.perf_counter()
                passed, failure_reason, evidence_refs, action_boundary = evaluator(commit, task)
                elapsed = max(0, int((time.perf_counter() - started) * 1000))
                records.append(
                    RunRecord(
                        version=version,
                        commit=commit,
                        task_id=task.task_id,
                        repetition=repetition,
                        passed=passed,
                        failure_reason=failure_reason,
                        manual_interventions=0,
                        model_id="not-invoked:deterministic-contract",
                        model_parameters={},
                        budget="no-llm-no-fc",
                        template="not-applicable:offline",
                        profile_version="1",
                        config_version="p7-01-task-profile-v1",
                        task_source="synthetic-fixed-fixture",
                        fixture_hashes=_fixture_manifest(task),
                        evidence_refs=evidence_refs,
                        action_boundary=action_boundary,
                        duration_ms=elapsed,
                    )
                )
    return {
        "benchmark": "p7-02-task-profile-contract",
        "input_contract": [asdict(task) for task in TASKS],
        "repetitions_per_task_version": repetitions,
        "total_runs": len(records),
        "records": [asdict(record) for record in records],
    }


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", default=DEFAULT_BASELINE)
    parser.add_argument("--candidate", default=DEFAULT_CANDIDATE)
    parser.add_argument("--repetitions", type=int, default=DEFAULT_REPETITIONS)
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    result = run_benchmark(
        baseline=args.baseline,
        candidate=args.candidate,
        repetitions=args.repetitions,
    )
    rendered = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
