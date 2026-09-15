"""H11 policy for model-candidate actions under an existing workspace scope.

The policy is deliberately pure: callers supply only Host-verified facts and
receive one decision.  Issuing grants, reserving budgets and recording events
remain at the H6 controller seam.
"""

from __future__ import annotations

from typing import Literal

CandidateDecision = Literal["deny", "confirm", "auto", "notice"]
CandidateOperation = Literal["define", "trial", "activate", "update", "call", "stop", "remove"]
CandidateInputClass = Literal["fixture", "task", "sensitive", "unknown"]
WorkspaceMode = Literal["restricted", "full"]


class CandidateAutonomyPolicy:
    """Map verified candidate facts to one H11 authorization decision.

    ``notice`` means the caller may proceed automatically but must emit the
    non-blocking activity record required by H11.  It is never evidence of a
    user confirmation.
    """

    def decide(
        self,
        *,
        access_mode: WorkspaceMode | str | None,
        operation: CandidateOperation | str,
        input_class: CandidateInputClass | str,
        budget_available: bool,
        candidate_trialled: bool,
        profile_enabled: bool = True,
        contract_compliant: bool = True,
    ) -> CandidateDecision:
        """Return the H11 decision without trusting model or browser claims."""
        if not profile_enabled or not contract_compliant or not budget_available:
            return "deny"
        if input_class in {"sensitive", "unknown"}:
            return "deny"
        if operation not in {"define", "trial", "activate", "update", "call", "stop", "remove"}:
            return "deny"
        if operation == "trial" and input_class != "fixture":
            return "deny"
        if operation in {"activate", "update", "call"} and not candidate_trialled:
            return "deny"
        if operation in {"define", "trial", "stop", "remove", "activate", "update"}:
            return "auto"
        if operation == "call" and input_class == "fixture":
            return "auto"
        if operation != "call" or input_class != "task":
            return "deny"
        if access_mode == "full":
            return "notice"
        # Invalid/missing access modes retain the existing default experience:
        # a single confirmation before a candidate sees real task data.
        return "confirm"
