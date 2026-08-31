from pathlib import Path

from devpilot.config import DevPilotConfig


def test_inspect_defaults_are_restricted() -> None:
    config = DevPilotConfig.model_validate(
        {
            "environment": "inspect",
            "allowed_roots": [Path("workspace-template")],
        }
    )

    assert config.bind_host == "127.0.0.1"
    assert config.restrict_to_workspace is True
    assert config.allow_builtin_exec is False
    assert config.allow_network is False
