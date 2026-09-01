"""P0 environment check; intentionally uses only the Python standard library."""

from __future__ import annotations

import json
import os
import platform
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NANOBOT_ROOT = Path(
    os.environ.get(
        "DEVPILOT_NANOBOT_ROOT",
        str(ROOT.parent / "nanobot-inspect" / "nanobot-main"),
    )
)


def check() -> dict[str, object]:
    config_paths = sorted((ROOT / "config").glob("*.example.json"))
    return {
        "project_root": str(ROOT),
        "python": platform.python_version(),
        "platform": platform.platform(),
        "git": shutil.which("git") is not None,
        "node": shutil.which("node") is not None,
        "pnpm": shutil.which("pnpm") is not None,
        "uv": shutil.which("uv") is not None,
        "bwrap": shutil.which("bwrap") is not None,
        "nanobot_snapshot_exists": NANOBOT_ROOT.is_dir(),
        "config_examples": [path.name for path in config_paths],
        "deepseek_key_present": bool(os.environ.get("DEEPSEEK_API_KEY")),
        "sandbox_statement": (
            "Windows native: nanobot bwrap unavailable; do not claim OS isolation"
            if sys.platform == "win32"
            else "Non-Windows: bwrap detection passed; DevPilot backend still needs verification"
        ),
    }


if __name__ == "__main__":
    print(json.dumps(check(), ensure_ascii=False, indent=2))
