"""Non-destructive DevPilot environment diagnostic.

This script deliberately does not start services, install packages, or print secrets.
It may create the configured local data directory and a short-lived write-test file.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import socket
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from pydantic import ValidationError

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from devpilot.config import DevPilotConfig  # noqa: E402


def load_config(path: Path) -> DevPilotConfig:
    with path.open(encoding="utf-8") as handle:
        return DevPilotConfig.model_validate(json.load(handle))


def check_url(url: str) -> str:
    try:
        with urllib.request.urlopen(url, timeout=1) as response:  # noqa: S310
            return f"ok ({response.status})"
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return f"unavailable ({type(exc).__name__})"


def check_gateway(url: str) -> str:
    return check_url(f"{url.rstrip('/')}/health")


def check_port(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    try:
        address = (parsed.hostname or "127.0.0.1", parsed.port or 80)
        with socket.create_connection(address, timeout=0.5):
            return "open"
    except OSError:
        return "closed"


def sandbox_capability(config: DevPilotConfig) -> dict[str, str]:
    system = platform.system()
    bwrap = shutil.which("bwrap")
    docker = shutil.which("docker")
    if system == "Windows":
        return {
            "requested": config.sandbox_backend,
            "detected": "none",
            "status": "unavailable",
            "reason": "Windows 原生环境不提供 Linux bwrap 隔离；路径限制不等于 OS Sandbox",
        }
    if config.sandbox_backend in {"bwrap", "auto"} and bwrap:
        return {
            "requested": config.sandbox_backend,
            "detected": "bwrap",
            "status": "available",
            "reason": "",
        }
    if config.sandbox_backend in {"docker", "auto"} and docker:
        return {
            "requested": config.sandbox_backend,
            "detected": "docker",
            "status": "available",
            "reason": "",
        }
    return {
        "requested": config.sandbox_backend,
        "detected": "none",
        "status": "unavailable",
        "reason": "未检测到可用的受控执行后端",
    }


def provider_capability() -> str:
    """Report provider configuration presence without exposing the credential."""
    if os.environ.get("DEEPSEEK_API_KEY"):
        return "configured (environment)"
    config_path_value = os.environ.get("DEVPILOT_NANOBOT_CONFIG")
    if not config_path_value:
        return "not checked (set DEVPILOT_NANOBOT_CONFIG or DEEPSEEK_API_KEY)"
    config_path = Path(config_path_value).expanduser()
    try:
        with config_path.open(encoding="utf-8") as handle:
            payload = json.load(handle)
        deepseek = payload.get("providers", {}).get("deepseek", {})
        return "configured (nanobot config)" if bool(deepseek.get("apiKey")) else "missing"
    except (OSError, json.JSONDecodeError, AttributeError):
        return "unavailable"


def run(config_path: Path) -> int:
    try:
        config = load_config(config_path)
    except (OSError, json.JSONDecodeError, ValidationError) as exc:
        print(f"FAIL config: {type(exc).__name__}")
        return 1

    data_dir = (
        (ROOT / config.data_dir).resolve()
        if not config.data_dir.is_absolute()
        else config.data_dir
    )
    workspace = [
        (ROOT / path).resolve() if not path.is_absolute() else path.resolve()
        for path in config.allowed_roots
    ]
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(prefix="doctor-", dir=data_dir, delete=True):
            pass
        data_status = "ok"
    except OSError as exc:
        data_status = f"fail ({type(exc).__name__})"

    results: dict[str, Any] = {
        "python": sys.version.split()[0],
        "os": f"{platform.system()} {platform.release()}",
        "provider": provider_capability(),
        "bind_host": config.bind_host,
        "restrict_to_workspace": config.restrict_to_workspace,
        "allow_builtin_exec": config.allow_builtin_exec,
        "allow_network": config.allow_network,
        "data_dir": str(data_dir),
        "data_dir_writable": data_status,
        "allowed_roots_exist": all(path.exists() for path in workspace),
        "nanobot_gateway": check_gateway(config.nanobot_gateway_url),
        "nanobot_gateway_port": check_port(config.nanobot_gateway_url),
        "nanobot_webui": check_url(config.nanobot_webui_url),
        "nanobot_webui_port": check_port(config.nanobot_webui_url),
        "sandbox": sandbox_capability(config),
        "required_sandbox": config.required_sandbox,
        "api_key_output": "never printed",
    }
    print(json.dumps(results, ensure_ascii=False, indent=2))
    service_statuses = (
        results["nanobot_gateway"].startswith("ok"),
        results["nanobot_webui"].startswith("ok"),
    )
    sandbox_status = results["sandbox"]["status"]
    sandbox_ok = not config.required_sandbox or sandbox_status == "available"
    checks_ok = (
        data_status == "ok"
        and results["allowed_roots_exist"]
        and all(service_statuses)
        and sandbox_ok
    )
    return 0 if checks_ok else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="DevPilot P0 non-destructive environment doctor")
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    raise SystemExit(run(args.config))
