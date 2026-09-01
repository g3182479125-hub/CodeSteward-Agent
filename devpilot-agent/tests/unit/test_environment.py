from __future__ import annotations

from scripts.check_environment import check


def test_environment_check_has_safe_fields() -> None:
    result = check()

    assert result["project_root"]
    assert isinstance(result["config_examples"], list)
    assert isinstance(result["deepseek_key_present"], bool)
    assert "key" not in str(result).lower() or result["deepseek_key_present"] is False


def test_environment_check_finds_nanobot_snapshot() -> None:
    result = check()

    assert result["nanobot_snapshot_exists"] is True
    assert "sandbox_statement" in result
