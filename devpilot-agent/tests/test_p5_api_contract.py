from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from devpilot.control_plane import ControlPlaneManager, create_app
from devpilot.paths import AllowedRoots
from devpilot.redaction import redact_sensitive_payload, redact_sensitive_text
from devpilot.storage import ProjectStore


def setup_app(tmp_path: Path) -> tuple[TestClient, ControlPlaneManager, Path]:
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
    return TestClient(create_app(manager=manager), client=("127.0.0.1", 50000)), manager, project


def make_preview(client: TestClient, project: Path) -> dict[str, object]:
    response = client.post(
        "/api/v1/change-sets/preview",
        json={
            "project_path": str(project),
            "patches": [
                {"relative_path": "main.py", "operation": "create", "content": "ok\n"}
            ],
        },
    )
    assert response.status_code == 201
    return response.json()


def test_api_v1_success_and_v1_compatibility_share_request_id_contract(tmp_path: Path) -> None:
    client, _manager, project = setup_app(tmp_path)
    preview = make_preview(client, project)
    request_id = str(preview["request_id"])

    assert request_id.startswith("req_")
    first = client.get(f"/api/v1/change-sets/{preview['change_set_id']}")
    second = client.get(f"/v1/change-sets/{preview['change_set_id']}")
    assert first.status_code == second.status_code == 200
    assert first.json()["change_set_id"] == second.json()["change_set_id"]
    assert first.json()["request_id"] == first.headers["X-Request-ID"]
    assert second.json()["request_id"] == second.headers["X-Request-ID"]


def test_request_id_is_bounded_and_unsafe_value_is_not_echoed(tmp_path: Path) -> None:
    client, _manager, project = setup_app(tmp_path)
    response = client.post(
        "/api/v1/change-sets/preview",
        headers={"X-Request-ID": '"<script>unsafe</script>"'},
        json={
            "project_path": str(project),
            "patches": [
                {"relative_path": "main.py", "operation": "create", "content": "ok\n"}
            ],
        },
    )

    assert response.status_code == 201
    assert "script" not in response.text
    assert response.json()["request_id"] == response.headers["X-Request-ID"]


def test_loopback_contract_rejects_forwarded_requests(tmp_path: Path) -> None:
    client, _manager, _project = setup_app(tmp_path)
    response = client.get("/healthz", headers={"X-Forwarded-For": "10.0.0.8"})
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "PATH_DENIED"


def test_secret_redaction_covers_provider_and_repository_credentials() -> None:
    github_token = "github_pat_" + "abcdefghijklmnopqrstuvwxyz1234"
    text = (
        'api_key=sk-provider-secret authorization: Bearer abc123 '
        + github_token
        + ' {"token":"json-secret"}'
    )
    redacted = redact_sensitive_text(text)
    assert "sk-provider-secret" not in redacted
    assert "Bearer abc123" not in redacted
    assert github_token not in redacted
    assert "json-secret" not in redacted
    payload = redact_sensitive_payload(
        {"AWS_ACCESS_KEY_ID": "AKIA1234567890ABCDEF", "token": "value"}
    )
    assert payload == {"AWS_ACCESS_KEY_ID": "[REDACTED]", "token": "[REDACTED]"}


def test_failed_idempotency_reservation_can_be_released(tmp_path: Path) -> None:
    store = ProjectStore(tmp_path / "data" / "devpilot.db")
    store.initialize()
    assert store.reserve_idempotency("retry-me", "request-hash")[0] == "new"
    store.abort_idempotency("retry-me")
    assert store.get_idempotency("retry-me") is None


def test_validation_and_domain_errors_use_stable_error_contract(tmp_path: Path) -> None:
    client, _manager, project = setup_app(tmp_path)
    invalid = client.post(
        "/api/v1/change-sets/preview",
        json={"project_path": str(project), "patches": []},
    )
    assert invalid.status_code == 422
    assert invalid.json()["error"]["code"] == "REQUEST_INVALID"
    assert invalid.json()["request_id"] == invalid.headers["X-Request-ID"]
    assert "traceback" not in invalid.text.lower()

    missing = client.get("/api/v1/change-sets/does-not-exist")
    assert missing.status_code == 409
    assert missing.json()["error"]["code"] == "CHANGESET_NOT_FOUND"
    assert missing.json()["request_id"] == missing.headers["X-Request-ID"]


def test_unexpected_errors_are_redacted(tmp_path: Path, monkeypatch) -> None:
    client, manager, _project = setup_app(tmp_path)

    def explode(_change_set_id: str) -> dict[str, object]:
        raise RuntimeError("secret traceback detail")

    monkeypatch.setattr(manager, "get", explode)
    response = TestClient(
        client.app, client=("127.0.0.1", 50000), raise_server_exceptions=False
    ).get(
        "/api/v1/change-sets/boom"
    )

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "INTERNAL_ERROR"
    assert "secret traceback detail" not in response.text
    assert response.json()["request_id"] == response.headers["X-Request-ID"]


def test_openapi_exposes_canonical_and_compatibility_routes(tmp_path: Path) -> None:
    client, _manager, _project = setup_app(tmp_path)
    paths = client.get("/openapi.json").json()["paths"]

    assert "/api/v1/change-sets/preview" in paths
    assert "/v1/change-sets/preview" in paths
    assert "/api/v1/runs/{run_id}/events" in paths
    assert "/api/v1/security/backends" in paths


def test_console_read_models_expose_projects_skills_backends_and_diff(
    tmp_path: Path,
) -> None:
    client, _manager, project = setup_app(tmp_path)
    projects = client.get("/api/v1/projects")
    assert projects.status_code == 200
    assert len(projects.json()["projects"]) == 1
    assert "real_path" not in projects.json()["projects"][0]

    registered = client.post("/api/v1/projects", json={"project_path": str(project)})
    assert registered.status_code == 201
    project_id = registered.json()["project_id"]
    assert client.get(f"/api/v1/projects/{project_id}").json()["trust_state"] == "trusted"
    assert "real_path" not in client.get(f"/api/v1/projects/{project_id}/security").json()
    assert "real_path" not in client.get(f"/v1/projects/{project_id}/security").json()

    preview = make_preview(client, project)
    public_preview = client.get(f"/api/v1/change-sets/{preview['change_set_id']}/preview")
    assert "project_path" not in public_preview.json()
    legacy_preview = client.get(f"/v1/change-sets/{preview['change_set_id']}/preview")
    assert legacy_preview.status_code == 200
    assert legacy_preview.json()["project_path"] == str(project)
    diff = client.get(f"/api/v1/change-sets/{preview['change_set_id']}/diff")
    assert diff.status_code == 200
    assert "main.py" in diff.text
    backend = client.get("/api/v1/security/backends").json()["backends"][0]
    assert backend["verified"] is False
    assert backend["status"] == "unavailable"
    assert "skills" in client.get("/api/v1/skills").json()


def test_canonical_diff_redacts_credential_shaped_content(tmp_path: Path) -> None:
    client, _manager, project = setup_app(tmp_path)
    response = client.post(
        "/api/v1/change-sets/preview",
        json={
            "project_path": str(project),
            "patches": [{
                "relative_path": "config.txt",
                "operation": "create",
                "content": "api_key=\"my secret value\"\n",
            }],
        },
    )
    assert response.status_code == 201
    change_set_id = response.json()["change_set_id"]
    public_diff = client.get(f"/api/v1/change-sets/{change_set_id}/diff")
    assert "my secret value" not in public_diff.text
    assert "[REDACTED]" in public_diff.text


def test_sse_events_are_ordered_and_resume_after_last_event_id(tmp_path: Path) -> None:
    client, manager, project = setup_app(tmp_path)
    preview = make_preview(client, project)
    run_id = manager.get_run_id(str(preview["change_set_id"]))
    assert run_id is not None

    initial = client.get(f"/api/v1/runs/{run_id}/events")
    assert initial.status_code == 200
    assert initial.headers["content-type"].startswith("text/event-stream")
    blocks = [block for block in initial.text.split("\n\n") if block.strip()]
    assert len(blocks) == 2
    ids = [
        int(next(line for line in block.splitlines() if line.startswith("id: "))[4:])
        for block in blocks
    ]
    assert ids == sorted(ids)
    assert all('"run_id":' in block for block in blocks)

    resumed = client.get(
        f"/api/v1/runs/{run_id}/events", headers={"Last-Event-ID": str(ids[0])}
    )
    assert resumed.status_code == 200
    assert f"id: {ids[0]}\n" not in resumed.text
    assert f"id: {ids[1]}\n" in resumed.text


def test_sse_rejects_invalid_last_event_id_without_leaking_details(tmp_path: Path) -> None:
    client, manager, project = setup_app(tmp_path)
    preview = make_preview(client, project)
    run_id = manager.get_run_id(str(preview["change_set_id"]))
    assert run_id is not None

    response = client.get(
        f"/api/v1/runs/{run_id}/events", headers={"Last-Event-ID": "not-an-id"}
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "REQUEST_INVALID"
