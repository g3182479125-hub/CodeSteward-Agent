from __future__ import annotations

import hashlib
import os
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from devpilot.domain import ChangeSetStatus, FileOperation
from devpilot.patches import (
    MAX_CHANGESET_FILES,
    MAX_PATCH_BYTES,
    MAX_PATCH_LINES,
    ApplyReceipt,
    FilePatch,
    PatchError,
    StructuredPatchService,
    TestAuthorizationGrant,
)
from devpilot.paths import AllowedRoots


def digest(content: str) -> str:
    return hashlib.sha256(content.encode()).hexdigest()


def make_project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    project.mkdir()
    return project


def make_service(project: Path) -> StructuredPatchService:
    return StructuredPatchService(allowed_roots=AllowedRoots.from_paths([project.parent]))


def grant_for(plan, *, expires_at: datetime | None = None) -> TestAuthorizationGrant:
    return TestAuthorizationGrant(
        change_set_id=plan.change_set.id,
        project_id=plan.project_id,
        patch_hash=plan.patch_hash,
        scope_hash=plan.scope_hash,
        expires_at=expires_at or datetime.now(UTC) + timedelta(minutes=5),
    )


def test_plan_contains_hashes_and_unified_diff(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    target = project / "src" / "main.py"
    target.parent.mkdir()
    target.write_bytes(b"return 1\n")
    service = make_service(project)

    plan = service.plan(
        project,
        project_id="project-1",
        patches=(
            FilePatch(
                "src/main.py",
                FileOperation.UPDATE,
                "return 2\n",
                expected_sha256=digest("return 1\n"),
            ),
        ),
    )

    assert len(plan.patch_hash) == 64
    assert len(plan.scope_hash) == 64
    assert "-return 1" in plan.diff
    assert "+return 2" in plan.diff
    assert plan.records[0].after_sha256 == digest("return 2\n")
    with pytest.raises(FrozenInstanceError):
        plan.patches[0].content = "mutated\n"  # type: ignore[misc]


def test_plan_allows_public_password_template_but_not_secret_file(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    (project / "shop" / "templates").mkdir(parents=True)
    service = make_service(project)

    plan = service.plan(
        project,
        patches=(
            FilePatch(
                "shop/templates/forgot_password.html",
                FileOperation.CREATE,
                "<form>Reset</form>\n",
            ),
        ),
    )
    assert plan.records[0].relative_path == "shop/templates/forgot_password.html"
    with pytest.raises(PatchError) as error:
        service.plan(
            project,
            patches=(
                FilePatch(
                    "shop/templates/secret_password.html", FileOperation.CREATE, "private\n"
                ),
            ),
        )
    assert error.value.code == "FORBIDDEN_FILE"
    with pytest.raises(PatchError) as error:
        service.plan(
            project,
            patches=(
                FilePatch(
                    "shop/templates/password_backup.html", FileOperation.CREATE, "private\n"
                ),
            ),
        )
    assert error.value.code == "FORBIDDEN_FILE"
    with pytest.raises(PatchError) as error:
        service.plan(
            project,
            patches=(FilePatch("forgot_password.html", FileOperation.CREATE, "private\n"),),
        )
    assert error.value.code == "FORBIDDEN_FILE"


def test_plan_rejects_invalid_preconditions(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    target = project / "main.py"
    target.write_bytes(b"old\n")
    service = make_service(project)

    with pytest.raises(PatchError) as error:
        service.plan(
            project,
            patches=(FilePatch("main.py", FileOperation.UPDATE, "new\n", "0" * 64),),
        )
    assert error.value.code == "PRECONDITION_HASH_MISMATCH"

    with pytest.raises(PatchError) as error:
        service.plan(
            project,
            patches=(
                FilePatch("created.py", FileOperation.CREATE, "new\n", expected_sha256="0" * 64),
            ),
        )
    assert error.value.code == "HASH_INVALID"

    with pytest.raises(PatchError) as error:
        service.plan(
            project,
            patches=(
                FilePatch(
                    "main.py",
                    FileOperation.DELETE,
                    "not allowed",
                    expected_sha256=digest("old\n"),
                ),
            ),
        )
    assert error.value.code == "PATCH_CONTENT_INVALID"


def test_apply_and_rollback_create_update_delete(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    updated = project / "src" / "main.py"
    updated.parent.mkdir()
    updated.write_bytes(b"old\n")
    removed = project / "remove.txt"
    removed.write_bytes(b"remove me\n")
    service = make_service(project)
    plan = service.plan(
        project,
        project_id="project-1",
        patches=(
            FilePatch("src/main.py", FileOperation.UPDATE, "new\n", digest("old\n")),
            FilePatch("created.txt", FileOperation.CREATE, "created\n"),
            FilePatch("remove.txt", FileOperation.DELETE, expected_sha256=digest("remove me\n")),
        ),
    )

    receipt = service.apply(plan, grant_for(plan))

    assert updated.read_text(encoding="utf-8") == "new\n"
    assert (project / "created.txt").read_text(encoding="utf-8") == "created\n"
    assert not removed.exists()
    assert plan.change_set.status is ChangeSetStatus.APPLIED
    assert isinstance(receipt, ApplyReceipt)

    service.rollback(receipt, plan)

    assert updated.read_text(encoding="utf-8") == "old\n"
    assert not (project / "created.txt").exists()
    assert removed.read_text(encoding="utf-8") == "remove me\n"
    assert plan.change_set.status is ChangeSetStatus.REVERTED


def test_missing_authorization_does_not_write(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    target = project / "main.py"
    target.write_bytes(b"old\n")
    service = make_service(project)
    plan = service.plan(
        project,
        patches=(FilePatch("main.py", FileOperation.UPDATE, "new\n", digest("old\n")),),
    )

    with pytest.raises(PatchError) as error:
        service.apply(plan, None)

    assert error.value.code == "AUTHORIZATION_REQUIRED"
    assert target.read_text(encoding="utf-8") == "old\n"
    assert plan.change_set.status is ChangeSetStatus.PLANNED


def test_expired_and_replayed_authorization_are_rejected(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    target = project / "main.py"
    target.write_bytes(b"old\n")
    service = make_service(project)
    plan = service.plan(
        project,
        patches=(FilePatch("main.py", FileOperation.UPDATE, "new\n", digest("old\n")),),
    )
    expired = grant_for(plan, expires_at=datetime.now(UTC) - timedelta(seconds=1))

    with pytest.raises(PatchError) as error:
        service.apply(plan, expired)
    assert error.value.code == "AUTHORIZATION_EXPIRED"

    active = grant_for(plan)
    service.apply(plan, active)
    with pytest.raises(PatchError) as error:
        service.apply(plan, active)
    assert error.value.code == "AUTHORIZATION_REPLAYED"


def test_stale_hash_is_zero_write_and_marks_apply_failed(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    target = project / "main.py"
    target.write_bytes(b"old\n")
    service = make_service(project)
    plan = service.plan(
        project,
        patches=(FilePatch("main.py", FileOperation.UPDATE, "new\n", digest("old\n")),),
    )
    target.write_bytes(b"changed elsewhere\n")

    with pytest.raises(PatchError) as error:
        service.apply(plan, grant_for(plan))

    assert error.value.code == "PRECONDITION_HASH_MISMATCH"
    assert target.read_text(encoding="utf-8") == "changed elsewhere\n"
    assert plan.change_set.status is ChangeSetStatus.APPLY_FAILED


@pytest.mark.parametrize(
    ("relative_path", "code"),
    [
        ("../outside.txt", "PATH_OUTSIDE_PROJECT"),
        (".env", "FORBIDDEN_FILE"),
        (".git/config", "FORBIDDEN_FILE"),
        (".GIT/config", "FORBIDDEN_FILE"),
        ("CON.txt", "WINDOWS_PATH_ALIAS"),
        ("src/main.py. ", "WINDOWS_PATH_ALIAS"),
        ("src/main.py:secret", "WINDOWS_PATH_ALIAS"),
    ],
)
def test_forbidden_paths_are_rejected(tmp_path: Path, relative_path: str, code: str) -> None:
    project = make_project(tmp_path)
    service = make_service(project)

    with pytest.raises(PatchError) as error:
        service.plan(
            project,
            patches=(FilePatch(relative_path, FileOperation.CREATE, "unsafe\n"),),
        )

    assert error.value.code == code


def test_binary_and_oversized_files_are_rejected(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    binary = project / "binary.dat"
    binary.write_bytes(b"\x00\x01")
    service = make_service(project)

    with pytest.raises(PatchError) as error:
        service.plan(
            project,
            patches=(FilePatch("binary.dat", FileOperation.UPDATE, "text\n", digest("\x00\x01")),),
        )
    assert error.value.code == "BINARY_FILE_FORBIDDEN"

    with pytest.raises(PatchError) as error:
        service.plan(
            project,
            patches=(FilePatch("large.txt", FileOperation.CREATE, "x" * (512 * 1024 + 1)),),
        )
    assert error.value.code == "FILE_LIMIT_EXCEEDED"


def test_changeset_byte_and_line_budgets_are_rejected(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    service = make_service(project)

    with pytest.raises(PatchError) as error:
        service.plan(
            project,
            patches=(FilePatch("large.txt", FileOperation.CREATE, "x" * (MAX_PATCH_BYTES + 1)),),
        )
    assert error.value.code == "PATCH_LIMIT_EXCEEDED"


def test_large_update_uses_diff_budget_not_full_post_image(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    before = "\n".join(f"line {index}" for index in range(12_000)) + "\n"
    target = project / "large-test.py"
    target.write_bytes(before.encode("utf-8"))
    after = before.replace("line 6000", "line 6000 updated", 1)
    service = make_service(project)

    plan = service.plan(
        project,
        patches=(
            FilePatch(
                "large-test.py",
                FileOperation.UPDATE,
                after,
                expected_sha256=digest(before),
            ),
        ),
    )

    assert len(after.encode("utf-8")) > MAX_PATCH_BYTES
    assert plan.records[0].after_sha256 == digest(after)


def test_large_update_diff_still_hits_byte_budget(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    before = "\n".join(f"before {index} " + "x" * 100 for index in range(500)) + "\n"
    target = project / "large-diff.py"
    target.write_bytes(before.encode("utf-8"))
    after = "\n".join(f"after {index} " + "y" * 100 for index in range(500)) + "\n"
    service = make_service(project)

    with pytest.raises(PatchError) as error:
        service.plan(
            project,
            patches=(
                FilePatch(
                    "large-diff.py",
                    FileOperation.UPDATE,
                    after,
                    expected_sha256=digest(before),
                ),
            ),
        )

    assert len(after.encode("utf-8")) < MAX_PATCH_BYTES
    assert error.value.code == "PATCH_LIMIT_EXCEEDED"


    too_many_lines = "x\n" * (MAX_PATCH_LINES + 1)
    with pytest.raises(PatchError) as error:
        service.plan(
            project,
            patches=(FilePatch("lines.txt", FileOperation.CREATE, too_many_lines),),
        )
    assert error.value.code == "PATCH_LIMIT_EXCEEDED"


def test_duplicate_and_file_count_limits_are_rejected(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    service = make_service(project)
    with pytest.raises(PatchError) as error:
        service.plan(
            project,
            patches=(
                FilePatch("a.txt", FileOperation.CREATE, "a"),
                FilePatch("a.txt", FileOperation.CREATE, "a"),
            ),
        )
    assert error.value.code == "PATCH_DUPLICATE_PATH"

    with pytest.raises(PatchError) as error:
        service.plan(
            project,
            patches=(
                FilePatch("src", FileOperation.CREATE, "file"),
                FilePatch("src/main.py", FileOperation.CREATE, "nested"),
            ),
        )
    assert error.value.code == "PATCH_OVERLAPPING_PATH"

    with pytest.raises(PatchError) as error:
        service.plan(
            project,
            patches=(
                FilePatch("Case.txt", FileOperation.CREATE, "a"),
                FilePatch("case.txt", FileOperation.CREATE, "b"),
            ),
        )
    assert error.value.code == "PATCH_DUPLICATE_PATH"

    patches = tuple(
        FilePatch(f"file-{index}.txt", FileOperation.CREATE, "x")
        for index in range(MAX_CHANGESET_FILES + 1)
    )
    with pytest.raises(PatchError) as error:
        service.plan(project, patches=patches)
    assert error.value.code == "PATCH_LIMIT_EXCEEDED"


def test_symlink_escape_is_rejected_when_supported(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    link = project / "linked"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlink creation unavailable: {exc}")
    service = make_service(project)

    with pytest.raises(PatchError) as error:
        service.plan(
            project,
            patches=(FilePatch("linked/file.txt", FileOperation.CREATE, "escape\n"),),
        )
    assert error.value.code == "SYMLINK_FORBIDDEN"


def test_dangling_symlink_target_is_rejected_when_supported(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    link = project / "dangling.txt"
    try:
        link.symlink_to(tmp_path / "missing.txt")
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlink creation unavailable: {exc}")
    service = make_service(project)

    with pytest.raises(PatchError) as error:
        service.plan(
            project,
            patches=(FilePatch("dangling.txt", FileOperation.CREATE, "replacement\n"),),
        )
    assert error.value.code == "SYMLINK_FORBIDDEN"


def test_invalid_operation_has_stable_error(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    service = make_service(project)

    with pytest.raises(PatchError) as error:
        service.plan(
            project,
            patches=(FilePatch("main.py", "rename", "unsafe\n"),),  # type: ignore[arg-type]
        )
    assert error.value.code == "PATCH_OPERATION_INVALID"


def test_apply_failure_rolls_back_already_replaced_files(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    first = project / "first.txt"
    first.write_bytes(b"first old\n")
    calls = 0

    def replace_once_then_fail(
        source: str | os.PathLike[str], target: str | os.PathLike[str]
    ) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated replace failure")
        os.replace(source, target)

    service = StructuredPatchService(
        allowed_roots=AllowedRoots.from_paths([project.parent]), replace=replace_once_then_fail
    )
    plan = service.plan(
        project,
        patches=(
            FilePatch("first.txt", FileOperation.UPDATE, "first new\n", digest("first old\n")),
            FilePatch("second.txt", FileOperation.CREATE, "second\n"),
        ),
    )

    with pytest.raises(PatchError) as error:
        service.apply(plan, grant_for(plan))

    assert error.value.code == "APPLY_FAILED"
    assert first.read_text(encoding="utf-8") == "first old\n"
    assert not (project / "second.txt").exists()
    assert plan.change_set.status is ChangeSetStatus.APPLY_FAILED


def test_rollback_stops_on_user_change(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    target = project / "main.py"
    target.write_bytes(b"old\n")
    service = make_service(project)
    plan = service.plan(
        project,
        patches=(FilePatch("main.py", FileOperation.UPDATE, "new\n", digest("old\n")),),
    )
    receipt = service.apply(plan, grant_for(plan))
    target.write_bytes(b"user changed after apply\n")

    with pytest.raises(PatchError) as error:
        service.rollback(receipt, plan)

    assert error.value.code == "ROLLBACK_CONFLICT"
    assert target.read_text(encoding="utf-8") == "user changed after apply\n"
