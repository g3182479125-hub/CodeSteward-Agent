"""Small baseline command line entry point."""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import sys


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="devpilot")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("check", help="run the non-secret P0 environment check")
    serve = subparsers.add_parser("serve", help="run the localhost approval control plane")
    serve.add_argument("--host", default=os.environ.get("DEVPILOT_BIND_HOST", "127.0.0.1"))
    serve.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("DEVPILOT_BIND_PORT", "18800")),
    )
    return parser


def run_check() -> int:
    result = {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "git_available": shutil.which("git") is not None,
        "bwrap_available": shutil.which("bwrap") is not None,
        "sandbox_note": (
            "Windows native does not provide nanobot bwrap OS isolation"
            if sys.platform == "win32"
            else "bwrap availability is detected; DevPilot backend verification is still required"
        ),
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def run_serve(*, host: str, port: int) -> int:
    if host not in {"127.0.0.1", "localhost", "::1"}:
        print("DevPilot control plane only permits a localhost bind.", file=sys.stderr)
        return 2
    if not 1024 <= port <= 65535:
        print("DevPilot control plane port must be between 1024 and 65535.", file=sys.stderr)
        return 2
    import uvicorn

    from .control_plane import create_app

    uvicorn.run(create_app(), host=host, port=port, log_level="info")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "check":
        return run_check()
    if args.command == "serve":
        return run_serve(host=args.host, port=args.port)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
