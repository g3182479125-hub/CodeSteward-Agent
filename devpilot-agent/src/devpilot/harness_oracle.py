"""Host-owned, deterministic H9 fixture contracts.

H9 may compare candidate code, but candidates must never supply the inputs or
the pass/fail rule that selects them. A host registers a named target contract,
and the evaluator only accepts exactly those six fixed cases. Unknown targets
fail closed instead of receiving a generic, easy-to-game heuristic.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass

from .candidate_runner import CandidateExecutionResult, CandidateInputRule
from .remote_loop_strategy import h9_fixed_evaluation_projection


class HarnessOracleContractError(ValueError):
    """Raised when a target has no host-owned H9 fixture contract."""


_CASE_NAMES = frozenset(
    {
        "target-1",
        "target-2",
        "holdout-1",
        "holdout-2",
        "regression-1",
        "regression-2",
    }
)


@dataclass(frozen=True)
class FixedCaseContract:
    """One immutable fixture input and expected JSON result."""

    input_data: Mapping[str, object]
    expected_output: object


class TargetHarnessCaseOracle:
    """Judge one named target against host-owned inputs and outputs."""

    def __init__(self, target_id: str, cases: Mapping[str, FixedCaseContract]) -> None:
        if set(cases) != _CASE_NAMES:
            raise ValueError("H9 target contract must contain the six fixed cases")
        self.target_id = target_id
        self._cases = dict(cases)

    def fixed_input(
        self,
        fixture: Mapping[str, object],
        rules: tuple[CandidateInputRule, ...],
    ) -> dict[str, object]:
        case_name = fixture.get("case")
        if not isinstance(case_name, str) or case_name not in self._cases:
            raise HarnessOracleContractError("H9 fixture case is invalid")
        input_data = dict(self._cases[case_name].input_data)
        allowed_fields = {rule[0] for rule in rules}
        if set(input_data) != allowed_fields:
            raise HarnessOracleContractError(
                "H9 target contract does not match candidate input rules"
            )
        return input_data

    def fixture_contract_hash(self, fixture: Mapping[str, object]) -> str:
        case_name = fixture.get("case")
        if not isinstance(case_name, str) or case_name not in self._cases:
            raise HarnessOracleContractError("H9 fixture case is invalid")
        contract = self._cases[case_name]
        payload = json.dumps(
            {
                "target_id": self.target_id,
                "case": case_name,
                "input": contract.input_data,
                "expected_output": contract.expected_output,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def __call__(
        self,
        _category: str,
        _version: str,
        _scenario: int,
        _repeat: int,
        fixture: Mapping[str, object],
        result: CandidateExecutionResult,
    ) -> Mapping[str, object]:
        case_name = fixture.get("case")
        if not isinstance(case_name, str) or case_name not in self._cases:
            raise HarnessOracleContractError("H9 fixture case is invalid")
        has_safety_receipt = (
            isinstance(result.side_effect_count, int)
            and not isinstance(result.side_effect_count, bool)
            and result.side_effect_count >= 0
            and isinstance(result.permission_violation, bool)
        )
        contract_passed = (
            has_safety_receipt
            and result.side_effect_count == 0
            and not result.permission_violation
            and result.status == "passed"
            and result.exit_code == 0
            and not result.output_truncated
            and result.error_code is None
        )
        permission_violation = not has_safety_receipt or bool(result.permission_violation) or bool(
            result.error_code
            and any(
                marker in result.error_code
                for marker in ("PERMISSION", "CAPABILITY", "NETWORK")
            )
        )
        return {
            "success": contract_passed
            and result.output == self._cases[case_name].expected_output,
            "contract_passed": contract_passed,
            "side_effect_count": result.side_effect_count if has_safety_receipt else 1,
            "permission_violation": permission_violation,
        }


class HarnessCaseOracleRegistry:
    """Resolve only explicitly registered target contracts."""

    def __init__(self, contracts: Mapping[str, TargetHarnessCaseOracle]) -> None:
        self._contracts = dict(contracts)

    def register(self, contract: TargetHarnessCaseOracle) -> None:
        """Register one host-owned contract before that target can be evaluated.

        This is deliberately a host API rather than a model tool: a candidate
        cannot install or replace the oracle that judges its own output.
        """
        if contract.target_id in self._contracts:
            raise HarnessOracleContractError(
                f"H9 target {contract.target_id!r} already has a fixed fixture contract"
            )
        self._contracts[contract.target_id] = contract

    def for_target(self, target_id: str) -> TargetHarnessCaseOracle:
        try:
            return self._contracts[target_id]
        except KeyError as exc:
            raise HarnessOracleContractError(
                f"H9 target {target_id!r} has no host-owned fixed fixture contract"
            ) from exc


def _discount_cases() -> dict[str, FixedCaseContract]:
    values = {
        "target-1": (100, 10),
        "target-2": (250, 20),
        "holdout-1": (120, 25),
        "holdout-2": (80, 5),
        "regression-1": (50, 0),
        "regression-2": (300, 50),
    }
    return {
        name: FixedCaseContract(
            input_data={"price": price, "discount_percent": percent},
            expected_output={"discounted_price": price - price * percent / 100},
        )
        for name, (price, percent) in values.items()
    }


def _loop_decision_cases() -> dict[str, FixedCaseContract]:
    expected = {
        "schema": "h8-remote-driver-projection-v1",
        "action": "request_model",
        "reason": "continue",
    }
    return {
        name: FixedCaseContract(
            input_data=h9_fixed_evaluation_projection({"case": name}),
            expected_output=expected,
        )
        for name in _CASE_NAMES
    }


def build_default_harness_case_oracle() -> HarnessCaseOracleRegistry:
    """Return conservative built-in contracts used by the H9 demonstration.

    New targets must be registered by host code before evaluation. Keeping the
    registry closed is intentional: model-generated source cannot weaken an
    expectation by changing its own manifest or input schema.
    """

    return HarnessCaseOracleRegistry(
        {
            "h9.discount-calculator": TargetHarnessCaseOracle(
                "h9.discount-calculator", _discount_cases()
            ),
            "h9.loop-decision": TargetHarnessCaseOracle(
                "h9.loop-decision", _loop_decision_cases()
            ),
        }
    )
