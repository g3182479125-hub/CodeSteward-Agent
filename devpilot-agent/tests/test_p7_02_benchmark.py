"""Tests for the deterministic P7-02 contract benchmark."""

import importlib.util
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).parents[1] / "scripts" / "p7_02_benchmark.py"
_SPEC = importlib.util.spec_from_file_location("p7_02_benchmark", _SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
benchmark = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = benchmark
_SPEC.loader.exec_module(benchmark)


def test_benchmark_is_fixed_to_three_task_families() -> None:
    assert [task.profile for task in benchmark.TASKS] == [
        "repo-analysis",
        "feature-delivery",
        "bug-fix",
    ]
    assert [task.fixture for task in benchmark.TASKS] == [
        "devpilot-agent/tests/fixtures/python-project",
        "devpilot-agent/tests/fixtures/python-project",
        "devpilot-agent/tests/fixtures/react-form-project",
    ]


def test_benchmark_runs_three_repetitions_for_both_versions() -> None:
    result = benchmark.run_benchmark(
        baseline="e8369151cba046007cc05621756ab702a1ec99f8",
        candidate="7465dacfa8c0a35a341b248e7564eb3e2b9ef1ce",
        repetitions=3,
    )

    assert result["total_runs"] == 18
    assert len(result["records"]) == 18
    assert (
        sum(record["passed"] for record in result["records"] if record["version"] == "candidate")
        == 9
    )
    assert (
        sum(record["passed"] for record in result["records"] if record["version"] == "baseline")
        == 0
    )
    assert all(record["manual_interventions"] == 0 for record in result["records"])
    assert all(
        record["model_id"] == "not-invoked:deterministic-contract"
        for record in result["records"]
    )


def test_benchmark_rejects_unbounded_repetition_count() -> None:
    with pytest.raises(ValueError, match="between 1 and 10"):
        benchmark.run_benchmark(repetitions=0)
