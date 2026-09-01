from pathlib import Path

from devpilot.skill_manager import publish_skill, validate_skill

SKILL = Path(__file__).parent / ".." / "skills" / "catalog" / "repo-analysis" / "SKILL.md"


def test_repo_analysis_skill_is_publishable_after_review() -> None:
    result = validate_skill(SKILL.resolve())

    assert result.valid is True
    assert result.name == "repo-analysis"
    assert result.version == "0.1.0"
    assert result.status == "active"
    assert result.reviewed is True


def test_skill_publication_is_atomic_and_hashable(tmp_path: Path) -> None:
    destination, digest = publish_skill(SKILL.resolve(), tmp_path)

    assert destination == tmp_path / "skills" / "repo-analysis" / "SKILL.md"
    assert len(digest) == 64
    assert destination.read_text(encoding="utf-8") == SKILL.read_text(encoding="utf-8")
