"""Configuration models shared by diagnostics and the P1 services."""

from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field


def _empty_paths() -> list[Path]:
    return []


class DevPilotConfig(BaseModel):
    """Safe local configuration used by the P0 doctor."""

    model_config = ConfigDict(extra="forbid")

    environment: str = Field(pattern=r"^(inspect|develop|sandboxed-demo)$")
    bind_host: str = "127.0.0.1"
    bind_port: int = Field(default=18800, ge=1024, le=65535)
    data_dir: Path = Path("./local-data")
    allowed_roots: list[Path] = Field(default_factory=_empty_paths)
    restrict_to_workspace: bool = True
    sandbox_backend: str = Field(default="auto", pattern=r"^(auto|none|bwrap|docker|fc-agent)$")
    required_sandbox: bool = False
    allow_network: bool = False
    allow_builtin_exec: bool = False
    max_concurrent_runs: int = Field(default=1, ge=1, le=3)
    nanobot_gateway_url: str = "http://127.0.0.1:18790"
    nanobot_webui_url: str = "http://127.0.0.1:8765"
