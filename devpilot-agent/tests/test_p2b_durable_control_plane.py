from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from devpilot.control_plane import ControlPlaneManager, create_app
from devpilot.domain import FileOperation
from devpilot.execution import FakeExecutionBackend
from devpilot.patches import FilePatch, PatchError
from devpilot.paths import AllowedRoots
from devpilot.storage import ProjectStore


def setup_manager(tmp_path: Path) -> tuple[ProjectStore, Path, str, ControlPlaneManager]:
    project = tmp_path / "project"
    project.mkdir()
    store = ProjectStore(tmp_path / "data" / "devpilot.db")
    store.initialize()
    roots = AllowedRoots.from_paths([tmp_path])
    root_id, fingerprint = roots.binding_for(project)
    project_id = store.ensure_project(
        str(project.resolve()), allowed_root_id=root_id, repo_fingerprint=fingerprint
    )
    store.trust_project(
        project_id, allowed_root_id=root_id, repo_fingerprint=fingerprint
    )
    manager = ControlPlaneManager(allowed_roots=roots, store=store)
    return store, project, project_id, manager


def test_preview_is_reconstructed_after_manager_restart(tmp_path: Path) -> None:
    store, project, project_id, first = setup_manager(tmp_path)
    preview = first.preview(
        project_path=project,
        project_id=project_id,
        patches=[FilePatch("main.py", FileOperation.CREATE, "new\n")],
    )

    second = ControlPlaneManager(allowed_roots=AllowedRoots.from_paths([tmp_path]), store=store)

    assert second.get(str(preview["change_set_id"])) == preview


def test_apply_is_idempotent_and_rollback_requires_new_capability(tmp_path: Path) -> None:
    store, project, project_id, first = setup_manager(tmp_path)
    preview = first.preview(
        project_path=project,
        project_id=project_id,
        patches=[FilePatch("main.py", FileOperation.CREATE, "new\n")],
    )
    change_set_id = str(preview["change_set_id"])
    apply_approval = first.approve(change_set_id)
    token = str(apply_approval["approval_token"])

    applied = first.apply(change_set_id, token, idempotency_key="apply-1")
    replay = first.apply(change_set_id, token, idempotency_key="apply-1")
    assert replay == applied
    assert (project / "main.py").read_text(encoding="utf-8") == "new\n"

    with pytest.raises(PatchError, match="APPROVAL_INVALID"):
        first.rollback(change_set_id, token, idempotency_key="rollback-1")

    second = ControlPlaneManager(allowed_roots=AllowedRoots.from_paths([tmp_path]), store=store)
    rollback_approval = second.approve(change_set_id, "r1.rollback")
    reverted = second.rollback(
        change_set_id,
        str(rollback_approval["approval_token"]),
        idempotency_key="rollback-1",
    )
    assert reverted["status"] == "reverted"
    assert not (project / "main.py").exists()


def test_revoke_and_idempotency_conflict_fail_closed(tmp_path: Path) -> None:
    store, project, project_id, manager = setup_manager(tmp_path)
    preview = manager.preview(
        project_path=project,
        project_id=project_id,
        patches=[FilePatch("main.py", FileOperation.CREATE, "new\n")],
    )
    change_set_id = str(preview["change_set_id"])
    approval = manager.approve(change_set_id)
    manager.revoke(change_set_id)
    with pytest.raises(PatchError, match="APPROVAL_INVALID"):
        manager.apply(change_set_id, str(approval["approval_token"]), idempotency_key="revoked")

    second = manager.preview(
        project_path=project,
        project_id=project_id,
        patches=[FilePatch("other.py", FileOperation.CREATE, "other\n")],
    )
    token = str(manager.approve(str(second["change_set_id"]))["approval_token"])
    manager.apply(str(second["change_set_id"]), token, idempotency_key="same")
    with pytest.raises(PatchError, match="IDEMPOTENCY_CONFLICT"):
        manager.apply(str(second["change_set_id"]), "different-token", idempotency_key="same")


def test_token_cannot_cross_changesets_and_expired_approval_is_rejected(tmp_path: Path) -> None:
    store, project, project_id, manager = setup_manager(tmp_path)
    first = manager.preview(
        project_path=project,
        project_id=project_id,
        patches=[FilePatch("one.py", FileOperation.CREATE, "one\n")],
    )
    second = manager.preview(
        project_path=project,
        project_id=project_id,
        patches=[FilePatch("two.py", FileOperation.CREATE, "two\n")],
    )
    token = str(manager.approve(str(first["change_set_id"]))["approval_token"])
    with pytest.raises(PatchError, match="APPROVAL_SCOPE_MISMATCH"):
        manager.apply(str(second["change_set_id"]), token, idempotency_key="cross-project")
    assert not (project / "one.py").exists()
    assert not (project / "two.py").exists()

    expired_manager = ControlPlaneManager(
        allowed_roots=AllowedRoots.from_paths([tmp_path]),
        store=store,
        token_ttl=timedelta(seconds=-1),
    )
    expired = expired_manager.approve(str(second["change_set_id"]))
    with pytest.raises(PatchError, match="APPROVAL_INVALID"):
        expired_manager.apply(
            str(second["change_set_id"]),
            str(expired["approval_token"]),
            idempotency_key="expired",
        )


def test_concurrent_apply_consumes_one_approval(tmp_path: Path) -> None:
    store, project, project_id, manager = setup_manager(tmp_path)
    preview = manager.preview(
        project_path=project,
        project_id=project_id,
        patches=[FilePatch("main.py", FileOperation.CREATE, "new\n")],
    )
    change_set_id = str(preview["change_set_id"])
    token = str(manager.approve(change_set_id)["approval_token"])

    def attempt(key: str) -> str:
        try:
            manager.apply(change_set_id, token, idempotency_key=key)
            return "applied"
        except PatchError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(attempt, ["concurrent-a", "concurrent-b"]))
    assert sorted(outcomes) == ["APPROVAL_INVALID", "applied"]
    assert (project / "main.py").read_text(encoding="utf-8") == "new\n"


def test_http_contract_uses_authorization_header_and_idempotency_key(tmp_path: Path) -> None:
    store, project, project_id, manager = setup_manager(tmp_path)
    client = TestClient(create_app(manager=manager), client=("127.0.0.1", 50000))
    preview = client.post(
        "/v1/change-sets/preview",
        json={
            "project_path": str(project),
            "project_id": project_id,
            "patches": [{"relative_path": "main.py", "operation": "create", "content": "ok\n"}],
        },
    )
    assert preview.status_code == 201
    change_set_id = preview.json()["change_set_id"]
    approval = client.post(f"/v1/change-sets/{change_set_id}/approve")
    token = approval.json()["approval_token"]

    missing = client.post(f"/v1/change-sets/{change_set_id}/apply", json={"approval_token": token})
    assert missing.status_code == 409
    assert missing.json()["error"]["code"] == "IDEMPOTENCY_REQUIRED"

    applied = client.post(
        f"/v1/change-sets/{change_set_id}/apply",
        headers={"Authorization": f"DevPilotGrant {token}", "Idempotency-Key": "http-apply"},
    )
    assert applied.status_code == 200
    assert applied.json()["status"] == "applied"
    replay = client.post(
        f"/v1/change-sets/{change_set_id}/apply",
        headers={"Authorization": f"DevPilotGrant {token}", "Idempotency-Key": "http-apply"},
    )
    assert replay.status_code == 200
    assert replay.json() == applied.json()
    assert "approval_token" not in json.dumps(applied.json())


def test_http_rejects_non_loopback_client(tmp_path: Path) -> None:
    _store, _project, _project_id, manager = setup_manager(tmp_path)
    client = TestClient(create_app(manager=manager), client=("10.0.0.8", 50000))

    response = client.get("/healthz")

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "PATH_DENIED"


def test_http_verification_requires_separate_capability_and_is_idempotent(tmp_path: Path) -> None:
    store, project, project_id, _unused = setup_manager(tmp_path)
    manager = ControlPlaneManager(
        allowed_roots=AllowedRoots.from_paths([tmp_path]),
        store=store,
        execution_backend=FakeExecutionBackend(stdout="verified"),
    )
    client = TestClient(create_app(manager=manager), client=("127.0.0.1", 50000))
    preview = client.post(
        "/v1/change-sets/preview",
        json={
            "project_path": str(project),
            "project_id": project_id,
            "patches": [
                {"relative_path": "main.py", "operation": "create", "content": "print(1)\n"}
            ],
        },
    )
    change_set_id = preview.json()["change_set_id"]
    apply_approval = client.post(f"/v1/change-sets/{change_set_id}/approve")
    client.post(
        f"/v1/change-sets/{change_set_id}/apply",
        headers={
            "Authorization": f"DevPilotGrant {apply_approval.json()['approval_token']}",
            "Idempotency-Key": "verify-apply",
        },
    )
    verify_approval = client.post(
        f"/v1/change-sets/{change_set_id}/approve",
        json={"capability": "r2.verify"},
    )
    verified = client.post(
        f"/v1/change-sets/{change_set_id}/verify",
        json={"profile": "python-syntax"},
        headers={
            "Authorization": f"DevPilotGrant {verify_approval.json()['approval_token']}",
            "Idempotency-Key": "verify-1",
        },
    )
    replay = client.post(
        f"/v1/change-sets/{change_set_id}/verify",
        json={"profile": "python-syntax"},
        headers={
            "Authorization": f"DevPilotGrant {verify_approval.json()['approval_token']}",
            "Idempotency-Key": "verify-1",
        },
    )

    assert verified.status_code == 200
    assert verified.json()["status"] == "verified"
    assert replay.status_code == 200
    assert replay.json() == verified.json()


def test_create_app_requires_alembic_head(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="Alembic"):
        create_app(
            allowed_roots=AllowedRoots.from_paths([tmp_path]),
            data_dir=tmp_path / "unmigrated",
        )
