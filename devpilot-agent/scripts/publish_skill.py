"""Validate and publish one local Skill into a nanobot workspace."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from devpilot.skill_manager import publish_skill, validate_skill  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Publish a reviewed local DevPilot Skill")
    parser.add_argument("--skill", type=Path, required=True)
    parser.add_argument("--workspace", type=Path, required=True)
    args = parser.parse_args()
    validation = validate_skill(args.skill)
    if not validation.valid:
        print("Skill validation failed:")
        for error in validation.errors:
            print(f"- {error}")
        return 1
    destination, digest = publish_skill(args.skill, args.workspace)
    print(f"Published {validation.name} to {destination}")
    print(f"sha256={digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
