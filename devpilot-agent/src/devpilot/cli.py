"""Small baseline command line entry point."""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import sys
import uuid


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
    subparsers.add_parser(
        "sandbox-check", help="run a harmless connectivity and lifecycle check in FC Sandbox"
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
    if os.environ.get("DEVPILOT_ENABLE_EXPERIMENTAL_CONTROL_PLANE") != "1":
        print(
            "Experimental control plane is disabled until the P2B security Gate passes.",
            file=sys.stderr,
        )
        return 2
    internal_auth_token = os.environ.get("DEVPILOT_CONTROL_PLANE_TOKEN", "")
    if len(internal_auth_token) < 32:
        print(
            "DEVPILOT_CONTROL_PLANE_TOKEN must contain at least 32 characters.",
            file=sys.stderr,
        )
        return 2
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


def run_sandbox_check() -> int:
    """Verify FC Sandbox credentials and lifecycle without touching a project."""
    from .execution import ExecutionError, ExecutionFile, ExecutionRequest
    from .fc_sandbox import build_fc_backend_from_environment

    request = ExecutionRequest(
        # A diagnostic invocation must always be a fresh lifecycle check.
        # Reusing a persisted run ID makes a healthy provider look broken with
        # FC_RUN_DUPLICATE after the first successful check.
        run_id=f"sandbox-check-{uuid.uuid4()}",
        project_id="sandbox-check",
        files=(
            ExecutionFile(
                "sandbox_check.py",
                b"print('devpilot-fc-sandbox-ok')\n",
            ),
        ),
        command="python3 /workspace/devpilot/sandbox_check.py",
        timeout_seconds=60,
        network_allowed=False,
    )
    try:
        result = build_fc_backend_from_environment().run(request)
    except ExecutionError as exc:
        print(json.dumps({"status": "error", "code": exc.code, "message": exc.message}))
        return 1
    print(
        json.dumps(
            {
                "status": (
                    "degraded"
                    if result.exit_code == 0 and result.network_policy == "provider-default"
                    else "ok"
                    if result.exit_code == 0
                    else "failed"
                ),
                "backend": result.backend,
                "sandbox_id": result.sandbox_id,
                "exit_code": result.exit_code,
                "stdout": result.stdout,
                "stderr": result.stderr,
                "duration_ms": result.duration_ms,
                "network_policy": result.network_policy,
                "network_isolation_proven": result.network_policy == "required",
            },
            ensure_ascii=False,
        )
    )
    return 0 if result.exit_code == 0 else 1


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "check":
        return run_check()
    if args.command == "serve":
        return run_serve(host=args.host, port=args.port)
    if args.command == "sandbox-check":
        return run_sandbox_check()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
