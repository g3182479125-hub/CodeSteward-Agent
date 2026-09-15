from devpilot.candidate_autonomy import CandidateAutonomyPolicy


def test_h11_policy_maps_existing_workspace_modes_to_candidate_decisions() -> None:
    policy = CandidateAutonomyPolicy()

    assert policy.decide(
        access_mode="restricted",
        operation="define",
        input_class="fixture",
        budget_available=True,
        candidate_trialled=False,
    ) == "auto"
    assert policy.decide(
        access_mode="restricted",
        operation="call",
        input_class="task",
        budget_available=True,
        candidate_trialled=True,
    ) == "confirm"
    assert policy.decide(
        access_mode="full",
        operation="call",
        input_class="task",
        budget_available=True,
        candidate_trialled=True,
    ) == "notice"


def test_h11_policy_never_lets_full_access_bypass_hard_stops() -> None:
    policy = CandidateAutonomyPolicy()

    assert policy.decide(
        access_mode="full",
        operation="call",
        input_class="sensitive",
        budget_available=True,
        candidate_trialled=True,
    ) == "deny"
    assert policy.decide(
        access_mode="full",
        operation="call",
        input_class="task",
        budget_available=False,
        candidate_trialled=True,
    ) == "deny"
    assert policy.decide(
        access_mode="full",
        operation="call",
        input_class="task",
        budget_available=True,
        candidate_trialled=False,
    ) == "deny"


def test_h11_policy_falls_back_safely_when_workspace_mode_or_data_class_is_unknown() -> None:
    policy = CandidateAutonomyPolicy()

    assert policy.decide(
        access_mode=None,
        operation="call",
        input_class="task",
        budget_available=True,
        candidate_trialled=True,
    ) == "confirm"
    assert policy.decide(
        access_mode="full",
        operation="call",
        input_class="unknown",
        budget_available=True,
        candidate_trialled=True,
    ) == "deny"
