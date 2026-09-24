"""Execution backend contracts for local or optional sandboxed verification.

The P2B control plane remains the authority for local ChangeSet approval and
file mutation. This module defines the execution seam: a caller supplies an
explicit, already-scoped file bundle and server-selected profile, and a backend
returns a bounded execution receipt that identifies where execution occurred.
"""

from __future__ import annotations

import shlex
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol, cast

MAX_EXECUTION_FILES = 200
MAX_EXECUTION_FILE_BYTES = 512 * 1024
MAX_EXECUTION_BUNDLE_BYTES = 8 * 1024 * 1024
MAX_EXECUTION_OUTPUT_BYTES = 16 * 1024
MAX_REQUEST_OUTPUT_BYTES = 64 * 1024
DEPENDENCY_PROFILES = frozenset({"", "python-requirements", "python-project", "node-npm-ci"})


class ExecutionError(RuntimeError):
    """Safe, stable error raised by an execution backend."""

    def __init__(
        self, code: str, message: str, *, details: Mapping[str, object] | None = None
    ) -> None:
        self.code = code
        self.message = message
        self.details = dict(details or {})
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True, slots=True)
class ExecutionFile:
    """A file explicitly selected for one verification snapshot."""

    relative_path: str
    content: bytes


@dataclass(frozen=True, slots=True)
class ExecutionRequest:
    """Fully specified input for one profile-bound verification run."""

    run_id: str
    project_id: str
    files: tuple[ExecutionFile, ...]
    command: str
    timeout_seconds: int = 300
    network_allowed: bool = False
    command_profile: str = ""
    max_output_bytes: int = MAX_EXECUTION_OUTPUT_BYTES
    working_directory: str = ""
    dependency_profile: str = ""
    dependency_fingerprint: str = ""
    prepared_environment_id: str | None = None


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    """Provider-neutral execution receipt."""

    backend: str
    sandbox_id: str
    exit_code: int
    stdout: str
    stderr: str
    duration_ms: int
    artifacts: tuple[str, ...] = ()
    command_profile: str = ""
    bundle_bytes: int = 0
    region: str = ""
    template: str = ""
    network_policy: str = ""
    output_truncated: bool = False
    # Backends must attest explicitly; None means the provider did not expose
    # enough information for a safety-sensitive promotion decision.
    side_effect_count: int | None = None
    permission_violation: bool | None = None
    # Set by the execution backend. Adapter/model metadata is not accepted as
    # proof that a non-zero exit is a semantic failure.
    failure_kind: str = "unknown"
    execution_location: str = "fc"


class ExecutionBackend(Protocol):
    """Port implemented by local and sandbox execution adapters."""

    name: str

    def run(self, request: ExecutionRequest) -> ExecutionResult:
        """Run one explicit verification request and return its receipt."""
        ...


def execution_receipt_location_is_consistent(receipt: ExecutionResult) -> bool:
    """Validate that location, backend, sandbox ID, and attestations agree."""

    if receipt.execution_location == "local":
        return (
            receipt.backend == "local"
            and not receipt.sandbox_id
            and receipt.side_effect_count is None
            and receipt.permission_violation is None
        )
    if receipt.execution_location == "fc":
        return (
            bool(receipt.backend)
            and receipt.backend != "local"
            and bool(receipt.sandbox_id)
            and receipt.side_effect_count == 0
            and receipt.permission_violation is False
        )
    return False


def validate_request(request: ExecutionRequest) -> None:
    """Reject malformed requests before any provider is contacted."""

    if not request.run_id or not request.project_id:
        raise ExecutionError("EXECUTION_SCOPE_INVALID", "run_id and project_id are required")
    if not request.files:
        raise ExecutionError("EXECUTION_INPUT_EMPTY", "at least one input file is required")
    if len(request.files) > MAX_EXECUTION_FILES:
        raise ExecutionError(
            "EXECUTION_FILE_LIMIT_EXCEEDED",
            f"at most {MAX_EXECUTION_FILES} input files are allowed",
        )
    if not request.command.strip():
        raise ExecutionError("EXECUTION_COMMAND_EMPTY", "a verification command is required")
    if request.dependency_profile not in DEPENDENCY_PROFILES:
        raise ExecutionError(
            "EXECUTION_DEPENDENCY_PROFILE_INVALID",
            "dependency profile is not server-allowlisted",
        )
    if request.working_directory:
        working_directory = request.working_directory.replace("\\", "/")
        parts = tuple(part for part in working_directory.split("/") if part)
        if (
            not parts
            or working_directory.startswith("/")
            or ".." in parts
            or any(":" in part for part in parts)
        ):
            raise ExecutionError(
                "EXECUTION_WORKING_DIRECTORY_INVALID",
                "working directory must be a relative uploaded path",
            )
    if not 1 <= request.timeout_seconds <= 900:
        raise ExecutionError(
            "EXECUTION_TIMEOUT_INVALID", "timeout must be between 1 and 900 seconds"
        )
    if (
        not isinstance(request.max_output_bytes, int)
        or isinstance(request.max_output_bytes, bool)
        or not 1 <= request.max_output_bytes <= MAX_REQUEST_OUTPUT_BYTES
    ):
        raise ExecutionError(
            "EXECUTION_OUTPUT_LIMIT_INVALID",
            f"output limit must be between 1 and {MAX_REQUEST_OUTPUT_BYTES} bytes",
        )
    seen: set[str] = set()
    bundle_bytes = 0
    normalized_files: set[str] = set()
    for item in request.files:
        path = item.relative_path.replace("\\", "/")
        parts = tuple(part for part in path.split("/") if part)
        if not parts or path.startswith("/") or ".." in parts or any(":" in part for part in parts):
            raise ExecutionError("EXECUTION_PATH_INVALID", "sandbox input paths must be relative")
        if path in seen:
            raise ExecutionError("EXECUTION_PATH_DUPLICATE", "sandbox input paths must be unique")
        seen.add(path)
        normalized_files.add(f"/workspace/devpilot/{path}")
        if len(item.content) > MAX_EXECUTION_FILE_BYTES:
            raise ExecutionError(
                "EXECUTION_FILE_LIMIT_EXCEEDED",
                f"input file {path} exceeds the 512 KiB limit",
            )
        bundle_bytes += len(item.content)
    if bundle_bytes > MAX_EXECUTION_BUNDLE_BYTES:
        raise ExecutionError(
            "EXECUTION_BUNDLE_LIMIT_EXCEEDED",
            f"input bundle exceeds the {MAX_EXECUTION_BUNDLE_BYTES // (1024 * 1024)} MiB limit",
        )
    validate_command(request.command, normalized_files)


def validate_command(command: str, input_paths: set[str]) -> None:
    """Allow only verification commands over files in the explicit bundle."""

    try:
        tokens = shlex.split(command, posix=True)
    except ValueError as exc:
        raise ExecutionError(
            "EXECUTION_COMMAND_INVALID", "verification command is malformed"
        ) from exc
    if any(token in {";", "&&", "||", "|", ">", ">>", "<", "`"} for token in tokens):
        raise ExecutionError("EXECUTION_COMMAND_FORBIDDEN", "shell operators are not allowed")

    if len(tokens) >= 4 and tokens[:3] == ["python3", "-m", "py_compile"]:
        paths = tokens[3:]
        if not paths or any(
            path not in input_paths or not path.casefold().endswith(".py") for path in paths
        ):
            raise ExecutionError(
                "EXECUTION_COMMAND_FORBIDDEN",
                "command is outside the Python syntax allowlist",
            )
        return
    if tokens in (
        ["python3", "-m", "pytest", "-q"],
        ["python", "-m", "pytest", "-q"],
    ):
        return
    if tokens in (
        ["python3", "-m", "unittest", "discover"],
        ["python", "-m", "unittest", "discover"],
    ):
        return
    if tokens == ["npm", "test"]:
        return
    if len(tokens) == 2 and tokens[0] in {"python3", "python"}:
        path = tokens[1]
        if path in input_paths and path.casefold().endswith(".py"):
            return
        raise ExecutionError(
            "EXECUTION_COMMAND_FORBIDDEN", "command is outside the Python run allowlist"
        )
    if len(tokens) >= 3 and tokens[:2] == ["node", "--check"]:
        paths = tokens[2:]
        if not paths or any(
            path not in input_paths or not path.casefold().endswith((".js", ".mjs", ".cjs"))
            for path in paths
        ):
            raise ExecutionError(
                "EXECUTION_COMMAND_FORBIDDEN",
                "command is outside the Node syntax allowlist",
            )
        return
    if len(tokens) >= 2 and tokens[0] == "react-static":
        paths = tokens[1:]
        if not paths or any(
            path not in input_paths or not path.casefold().endswith(".jsx") for path in paths
        ):
            raise ExecutionError(
                "EXECUTION_COMMAND_FORBIDDEN",
                "command is outside the React static contract allowlist",
            )
        return
    raise ExecutionError(
        "EXECUTION_COMMAND_FORBIDDEN",
        "only allowlisted verification commands are accepted",
    )


def execution_bundle_bytes(request: ExecutionRequest) -> int:
    """Return the byte count after the request has passed structural validation."""

    return sum(len(item.content) for item in request.files)


class FakeExecutionBackend:
    """Deterministic backend used by contract tests without running commands."""

    name = "fake"

    def __init__(self, *, exit_code: int = 0, stdout: str = "fake verification") -> None:
        self.exit_code = exit_code
        self.stdout = stdout
        self.requests: list[ExecutionRequest] = []

    def run(self, request: ExecutionRequest) -> ExecutionResult:
        validate_request(request)
        self.requests.append(request)
        exit_code = self.exit_code
        stderr = ""
        if request.command_profile == "react-static":
            # G-02 is deliberately deterministic: Fake verification checks
            # the one required repair invariant instead of pretending that an
            # arbitrary configured exit code exercised React.
            if any(
                item.relative_path.casefold().endswith(".jsx")
                and b"event.preventDefault()" not in item.content
                for item in request.files
            ):
                exit_code = 1
                stderr = "react-static contract: event.preventDefault() is missing"
        stdout = self.stdout
        stderr_bytes = stderr.encode("utf-8")
        stdout_bytes = stdout.encode("utf-8")
        output_truncated = (
            len(stdout_bytes) > request.max_output_bytes
            or len(stderr_bytes) > request.max_output_bytes
        )
        if len(stdout_bytes) > request.max_output_bytes:
            stdout = stdout_bytes[-request.max_output_bytes :].decode("utf-8", errors="replace")
        if len(stderr_bytes) > request.max_output_bytes:
            stderr = stderr_bytes[-request.max_output_bytes :].decode("utf-8", errors="replace")
        return ExecutionResult(
            backend=self.name,
            sandbox_id="fake-sandbox",
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            duration_ms=0,
            command_profile=request.command_profile,
            bundle_bytes=execution_bundle_bytes(request),
            output_truncated=output_truncated,
            side_effect_count=0,
            permission_violation=False,
            failure_kind="semantic",
        )


def request_from_mapping(payload: Mapping[str, object]) -> ExecutionRequest:
    """Convert a trusted internal mapping into the typed execution request."""

    raw_files = payload.get("files")
    if not isinstance(raw_files, Sequence) or isinstance(raw_files, (str, bytes, bytearray)):
        raise ExecutionError("EXECUTION_INPUT_INVALID", "files must be a sequence")
    files: list[ExecutionFile] = []
    for raw in cast(Sequence[object], raw_files):
        if not isinstance(raw, Mapping):
            raise ExecutionError("EXECUTION_INPUT_INVALID", "each file must be an object")
        raw_mapping = cast(Mapping[str, object], raw)
        relative_path = raw_mapping.get("relative_path")
        content = raw_mapping.get("content")
        if not isinstance(relative_path, str) or not isinstance(content, (bytes, bytearray)):
            raise ExecutionError("EXECUTION_INPUT_INVALID", "file path and bytes are required")
        files.append(ExecutionFile(relative_path, bytes(content)))
    run_id = payload.get("run_id")
    project_id = payload.get("project_id")
    command = payload.get("command")
    if not (isinstance(run_id, str) and isinstance(project_id, str) and isinstance(command, str)):
        raise ExecutionError(
            "EXECUTION_INPUT_INVALID", "run_id, project_id and command are required"
        )
    timeout = payload.get("timeout_seconds", 300)
    if not isinstance(timeout, int):
        raise ExecutionError("EXECUTION_INPUT_INVALID", "timeout_seconds must be an integer")
    network_allowed = payload.get("network_allowed", False)
    if not isinstance(network_allowed, bool):
        raise ExecutionError("EXECUTION_INPUT_INVALID", "network_allowed must be a boolean")
    max_output_bytes = payload.get("max_output_bytes", MAX_EXECUTION_OUTPUT_BYTES)
    if not isinstance(max_output_bytes, int) or isinstance(max_output_bytes, bool):
        raise ExecutionError("EXECUTION_INPUT_INVALID", "max_output_bytes must be an integer")
    prepared_environment_id = payload.get("prepared_environment_id")
    if prepared_environment_id is not None and not isinstance(prepared_environment_id, str):
        raise ExecutionError("EXECUTION_INPUT_INVALID", "prepared_environment_id must be a string")
    dependency_fingerprint = payload.get("dependency_fingerprint", "")
    if not isinstance(dependency_fingerprint, str):
        raise ExecutionError("EXECUTION_INPUT_INVALID", "dependency_fingerprint must be a string")
    return ExecutionRequest(
        run_id=run_id,
        project_id=project_id,
        files=tuple(files),
        command=command,
        timeout_seconds=timeout,
        network_allowed=network_allowed,
        command_profile=str(payload.get("command_profile", "")),
        max_output_bytes=max_output_bytes,
        working_directory=str(payload.get("working_directory", "")),
        dependency_profile=str(payload.get("dependency_profile", "")),
        dependency_fingerprint=dependency_fingerprint,
        prepared_environment_id=prepared_environment_id,
    )
