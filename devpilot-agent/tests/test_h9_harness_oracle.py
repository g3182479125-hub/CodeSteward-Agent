from dataclasses import replace

import pytest

from devpilot.candidate_runner import CandidateExecutionResult
from devpilot.harness_oracle import (
    FixedCaseContract,
    HarnessCaseOracleRegistry,
    HarnessOracleContractError,
    TargetHarnessCaseOracle,
    build_default_harness_case_oracle,
)


def _result(*, output: object, error_code: str | None = None) -> CandidateExecutionResult:
    return CandidateExecutionResult(
        status="passed" if error_code is None else "failed",
        candidate_id="h9.discount-calculator",
        version="1.1.0",
        trial_id="h9-oracle-test",
        run_id="run-1",
        backend="test",
        sandbox_id="sandbox-1",
        exit_code=0 if error_code is None else 1,
        output=output if error_code is None else None,
        error_code=error_code,
        error_message=None,
        duration_ms=1,
        cleanup_status="cleaned",
        source_hash="a" * 64,
        input_hash="b" * 64,
        side_effect_count=0,
        permission_violation=False,
    )


def test_h9_host_oracle_has_no_fallback_for_an_unregistered_target() -> None:
    oracle = build_default_harness_case_oracle()

    with pytest.raises(HarnessOracleContractError, match="no host-owned fixed fixture contract"):
        oracle.for_target("model-created-unknown-tool")


def test_h9_host_code_can_register_a_new_fixed_contract_once() -> None:
    registry = HarnessCaseOracleRegistry({})
    contract = TargetHarnessCaseOracle(
        "host-registered-target",
        {
            name: FixedCaseContract(input_data={"case": name}, expected_output={"ok": name})
            for name in (
                "target-1", "target-2", "holdout-1", "holdout-2", "regression-1", "regression-2"
            )
        },
    )

    registry.register(contract)

    assert registry.for_target("host-registered-target") is contract
    with pytest.raises(HarnessOracleContractError, match="already has a fixed fixture contract"):
        registry.register(contract)


def test_h9_host_oracle_rejects_a_permission_violation_even_if_output_matches() -> None:
    oracle = build_default_harness_case_oracle().for_target("h9.discount-calculator")
    judged = oracle(
        "target",
        "candidate",
        1,
        1,
        {"case": "target-1"},
        _result(output={"discounted_price": 90}, error_code="CANDIDATE_NETWORK_DENIED"),
    )

    assert judged["success"] is False
    assert judged["contract_passed"] is False
    assert judged["permission_violation"] is True


def test_h9_host_oracle_propagates_a_runner_side_effect_receipt() -> None:
    oracle = build_default_harness_case_oracle().for_target("h9.discount-calculator")
    result = _result(output={"discounted_price": 90})
    judged = oracle(
        "target",
        "candidate",
        1,
        1,
        {"case": "target-1"},
        replace(result, side_effect_count=1),
    )

    assert judged["side_effect_count"] == 1


def test_h9_host_oracle_fails_closed_when_runner_omits_a_safety_receipt() -> None:
    oracle = build_default_harness_case_oracle().for_target("h9.discount-calculator")
    judged = oracle(
        "target",
        "candidate",
        1,
        1,
        {"case": "target-1"},
        replace(_result(output={"discounted_price": 90}), side_effect_count=None),
    )

    assert judged["success"] is False
    assert judged["contract_passed"] is False
    assert judged["permission_violation"] is True
