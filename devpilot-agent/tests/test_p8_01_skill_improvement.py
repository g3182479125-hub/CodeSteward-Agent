from pathlib import Path

import pytest

from devpilot.skill_improvement import (
    SkillImprovementError,
    SkillImprovementStore,
    content_sha256,
)


def _baseline() -> str:
    return """---\nname: bug-fix\ndescription: bounded fix\nmetadata:\n  devpilot:\n    status: active\n    reviewed: true\n---\n\n# Bug fix\n\n## 执行步骤\n\n1. Read the failure evidence.\n"""


def _store(tmp_path: Path) -> tuple[SkillImprovementStore, str]:
    store = SkillImprovementStore(tmp_path / "data")
    feedback = store.record_feedback(
        task_id="task-1",
        project_id="project-1",
        source_type="user_feedback",
        skill_id="bug-fix",
        skill_hash=content_sha256(_baseline()),
        correction="先验证失败证据，再提出修改。",
        actual_result="用户确认原流程漏掉了失败证据校验。",
        evidence_refs=["run-1"],
    )
    return store, feedback.evidence_id


def test_feedback_is_redacted_and_candidate_stays_outside_skill_loader(tmp_path: Path) -> None:
    store, evidence_id = _store(tmp_path)
    feedback = store.get_feedback(evidence_id)
    assert feedback is not None
    assert feedback.processing_state == "verified"

    candidate = store.create_candidate(
        project_id="project-1",
        skill_path="skills/bug-fix/SKILL.md",
        baseline_content=_baseline(),
        candidate_content=_baseline() + "\n- Re-read the current file before proposing a patch.\n",
        evidence_ids=[evidence_id],
        reason="The failed task did not re-check the source file.",
        expected_behavior="The proposal cites fresh file evidence.",
        test_set_version="p8-01-v1",
    )
    assert candidate.status == "pending_evaluation"
    assert candidate.candidate_hash != candidate.base_version_hash
    assert candidate.candidate_content
    assert not (tmp_path / "data" / "skills" / "bug-fix" / "SKILL.md").exists()


def test_model_inference_requires_independent_verification(tmp_path: Path) -> None:
    store = SkillImprovementStore(tmp_path / "data")
    with pytest.raises(SkillImprovementError, match="two independent"):
        store.record_feedback(
            task_id="task-1",
            project_id="project-1",
            source_type="model_inference",
            skill_id="bug-fix",
            skill_hash="a" * 64,
            correction="inference",
            actual_result="unverified",
            evidence_refs=["one"],
        )


def test_candidate_cannot_change_frontmatter_or_include_secret(tmp_path: Path) -> None:
    store, evidence_id = _store(tmp_path)
    with pytest.raises(SkillImprovementError, match="protected metadata"):
        store.create_candidate(
            project_id="project-1",
            skill_path="skills/bug-fix/SKILL.md",
            baseline_content=_baseline(),
            candidate_content=_baseline().replace("status: active", "status: enabled"),
            evidence_ids=[evidence_id],
            reason="reason",
            expected_behavior="behavior",
            test_set_version="p8-01-v1",
        )

    with pytest.raises(SkillImprovementError, match="sensitive"):
        store.create_candidate(
            project_id="project-1",
            skill_path="skills/bug-fix/SKILL.md",
            baseline_content=_baseline(),
            candidate_content=_baseline() + "\napi_key=sk-12345678901234567890\n",
            evidence_ids=[evidence_id],
            reason="reason",
            expected_behavior="behavior",
            test_set_version="p8-01-v1",
        )


def test_candidate_batch_is_bounded_to_three(tmp_path: Path) -> None:
    store, evidence_id = _store(tmp_path)
    batch = "batch-fixed"
    for index in range(3):
        store.create_candidate(
            project_id="project-1",
            skill_path="skills/bug-fix/SKILL.md",
            baseline_content=_baseline(),
            candidate_content=_baseline() + f"\n- Rule {index}\n",
            evidence_ids=[evidence_id],
            reason="reason",
            expected_behavior="behavior",
            test_set_version="p8-01-v1",
            batch_id=batch,
        )
    with pytest.raises(SkillImprovementError, match="limit is three"):
        store.create_candidate(
            project_id="project-1",
            skill_path="skills/bug-fix/SKILL.md",
            baseline_content=_baseline(),
            candidate_content=_baseline() + "\n- Rule 4\n",
            evidence_ids=[evidence_id],
            reason="reason",
            expected_behavior="behavior",
            test_set_version="p8-01-v1",
            batch_id=batch,
        )
