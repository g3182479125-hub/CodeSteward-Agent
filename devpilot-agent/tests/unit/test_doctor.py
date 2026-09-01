from pathlib import Path

import scripts.doctor as doctor


def test_provider_diagnostic_does_not_require_or_print_a_key(monkeypatch) -> None:
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("DEVPILOT_NANOBOT_CONFIG", raising=False)

    result = doctor.provider_capability()

    assert result.startswith("not checked")
    assert "sk-" not in result.lower()


def test_closed_port_is_reported_as_closed() -> None:
    assert doctor.check_port("http://127.0.0.1:1") == "closed"


def test_windows_native_sandbox_is_explicitly_unavailable(monkeypatch) -> None:
    monkeypatch.setattr(doctor.platform, "system", lambda: "Windows")

    result = doctor.sandbox_capability(
        doctor.DevPilotConfig(environment="inspect", sandbox_backend="auto")
    )

    assert result["status"] == "unavailable"
    assert "OS Sandbox" in result["reason"]


def test_fc_agent_reports_configuration_without_exposing_key(monkeypatch) -> None:
    monkeypatch.setenv("E2B_API_KEY", "secret-value")
    monkeypatch.setenv("E2B_API_URL", "https://api.cn-shanghai.e2b.fc.aliyuncs.com")
    monkeypatch.setenv("E2B_DOMAIN", "cn-shanghai.e2b.fc.aliyuncs.com")

    result = doctor.sandbox_capability(
        doctor.DevPilotConfig(environment="develop", sandbox_backend="fc-agent")
    )

    assert result["status"] == "configured"
    assert "secret" not in str(result).lower()


def test_unwritable_data_dir_fails_diagnostic(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "workspace").mkdir()
    (tmp_path / "not-a-directory").write_text("fixture", encoding="utf-8")
    config_path = tmp_path / "inspect.json"
    config_path.write_text(
        '{"environment":"inspect","data_dir":"not-a-directory",'
        '"allowed_roots":["workspace"],"nanobot_gateway_url":"http://127.0.0.1:1",'
        '"nanobot_webui_url":"http://127.0.0.1:1"}',
        encoding="utf-8",
    )
    monkeypatch.setattr(doctor, "ROOT", tmp_path)

    assert doctor.run(config_path) == 1
