"""Run the deterministic P7-02 task-profile contract benchmark.

This benchmark intentionally does not call an LLM, FC, or a user project.  It
measures the stable routing and safety contract introduced by P7-01 so that
the baseline and candidate use the same input, budget, and repetitions.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
NANOBOT_ROOT = REPO_ROOT / "nanobot"
DEFAULT_BASELINE = "e8369151cba046007cc05621756ab702a1ec99f8"
DEFAULT_CANDIDATE = "HEAD"
DEFAULT_REPETITIONS = 3

_TASK_PROFILE_SPEC = importlib.util.spec_from_file_location(
    "nanobot.agent.task_profiles", NANOBOT_ROOT / "nanobot" / "agent" / "task_profiles.py"
)
if _TASK_PROFILE_SPEC is None or _TASK_PROFILE_SPEC.loader is None:
    raise RuntimeError("nanobot task profile module is unavailable")
_TASK_PROFILE_MODULE = importlib.util.module_from_spec(_TASK_PROFILE_SPEC)
sys.modules[_TASK_PROFILE_SPEC.name] = _TASK_PROFILE_MODULE
_TASK_PROFILE_SPEC.loader.exec_module(_TASK_PROFILE_MODULE)
TaskProfileCatalog = _TASK_PROFILE_MODULE.TaskProfileCatalog
TaskProfileError = _TASK_PROFILE_MODULE.TaskProfileError


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
    budget: str
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


def _baseline_contract(commit: str, task: BenchmarkTask) -> tuple[bool, str | None]:
    """Probe the pre-P7 tree without importing candidate code into it."""
    profile_module = _git_show(commit, "nanobot/nanobot/agent/task_profiles.py")
    registry = _git_show(commit, "nanobot/nanobot/agent/tools/registry.py")
    session_manager = _git_show(commit, "nanobot/nanobot/session/manager.py")
    if profile_module:
        return False, "baseline unexpectedly contains task profile runtime"
    if "def restricted_to(" in registry:
        return False, "baseline unexpectedly contains isolated tool view"
    if "allowed_tools: frozenset[str] | None" in session_manager:
        return False, "baseline unexpectedly contains task policy binding"
    return False, f"pre-P7 baseline has no trusted {task.profile} task contract"


def _candidate_contract(commit: str, task: BenchmarkTask) -> tuple[bool, str | None]:
    if commit != _git_rev_parse("HEAD"):
        return False, "candidate must match the checked-out worktree"
    catalog = TaskProfileCatalog()
    profile = catalog.get(task.profile)
    registered = set(profile.allowed_tools)
    skills = set(profile.skill_names)
    try:
        catalog.validate_runtime(profile, registered, skills)
    except TaskProfileError as exc:
        return False, str(exc)
    if task.profile == "repo-analysis":
        if profile.allowed_tools != frozenset({"devpilot_inspect_project"}):
            return False, "project explanation exposes a non-read-only tool"
    elif "devpilot_confirmed_action" not in profile.allowed_tools:
        return False, "code task lacks the existing confirmed-action boundary"
    return True, None


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
                passed, failure_reason = evaluator(commit, task)
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
                        budget="no-llm-no-fc",
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
