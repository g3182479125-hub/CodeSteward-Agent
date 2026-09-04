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
    prefixed = redact_sensitive_text(
        "OPENAI_API_KEY=prefix-secret\nAWS_SECRET_ACCESS_KEY=aws-secret\n"
    )
    assert "prefix-secret" not in prefixed
    assert "aws-secret" not in prefixed
    multiline = redact_sensitive_text('token = """\nMULTILINE-SECRET\n"""')
    assert "MULTILINE-SECRET" not in multiline
    assert "[REDACTED]" in multiline
    camel_case = redact_sensitive_text(
        'const accessToken = "camel-secret"; const authToken="auth-secret";'
    )
    assert "camel-secret" not in camel_case
    assert "auth-secret" not in camel_case
    other_camel_case = redact_sensitive_text(
        'openaiApiKey = "openai-secret"; clientApiToken="client-secret"; '
        'githubToken="github-secret"; secretKey="key-secret"; dbPassword="db-secret";'
    )
    for secret in (
        "openai-secret",
        "client-secret",
        "github-secret",
        "key-secret",
        "db-secret",
    ):
        assert secret not in other_camel_case
    for assignment, secret in (
        ("secret=plain-secret", "plain-secret"),
        ("client_secret=client-secret", "client-secret"),
        ("private_key=private-secret", "private-secret"),
        ("credential=credential-secret", "credential-secret"),
    ):
        assert secret not in redact_sensitive_text(assignment)
    aws_access_key = "AKIA" + "1234567890" + "ABCDEF"
    payload = redact_sensitive_payload(
        {
            "AWS_ACCESS_KEY_ID": aws_access_key,
            "token": "value",
            "api-key": "hyphen-api-key",
            "private-key": "hyphen-private-key",
        }
    )
    assert payload == {
        "AWS_ACCESS_KEY_ID": "[REDACTED]",
        "token": "[REDACTED]",
        "api-key": "[REDACTED]",
        "private-key": "[REDACTED]",
    }


def test_nanobot_adapter_removes_control_plane_metadata() -> None:
    from devpilot.nanobot_tools import _model_result

    payload = _model_result(
        {"request_id": "req-secret", "approval_token": "capability", "status": "ok"}
    )

    assert payload == {"status": "ok"}


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

    not_found = client.get("/api/v1/does-not-exist")
    assert not_found.status_code == 404
    assert not_found.json()["error"]["code"] == "ROUTE_NOT_FOUND"
    assert not_found.json()["request_id"] == not_found.headers["X-Request-ID"]

    method_not_allowed = client.post("/api/v1/skills")
    assert method_not_allowed.status_code == 405
    assert method_not_allowed.json()["error"]["code"] == "METHOD_NOT_ALLOWED"
    assert method_not_allowed.json()["request_id"] == method_not_allowed.headers["X-Request-ID"]

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
    openapi = client.get("/openapi.json").json()
    paths = openapi["paths"]

    assert "/api/v1/change-sets/preview" in paths
    assert "/v1/change-sets/preview" in paths
    assert "/api/v1/runs/{run_id}/events" in paths
    assert "/api/v1/security/backends" in paths
    assert "project_path" in openapi["components"]["schemas"]["PreviewRequest"]["properties"]
    assert "request_id" not in openapi


def test_request_id_body_and_header_stay_identical_for_credential_shaped_id(
    tmp_path: Path,
) -> None:
    client, _manager, _project = setup_app(tmp_path)
    request_id = "sk-abcdefghijkl"

    response = client.get("/api/v1/does-not-exist", headers={"X-Request-ID": request_id})

    assert response.status_code == 404
    assert response.headers["X-Request-ID"] == request_id
    assert response.json()["request_id"] == request_id


def test_idempotency_request_id_stays_stable_across_caller_ids(tmp_path: Path) -> None:
    client, _manager, _project = setup_app(tmp_path)
    key = "stable-key"

    first = client.get(
        "/healthz",
        headers={"X-Request-ID": "caller-request-1", "Idempotency-Key": key},
    )
    replay = client.get("/healthz", headers={"Idempotency-Key": key})

    assert first.status_code == replay.status_code == 200
    assert first.headers["X-Request-ID"] == replay.headers["X-Request-ID"]
    assert first.json()["request_id"] == replay.json()["request_id"]


def test_error_response_does_not_expose_project_path(tmp_path: Path) -> None:
    client, _manager, project = setup_app(tmp_path)
    project_id = client.get("/api/v1/projects").json()["projects"][0]["project_id"]
    project.rmdir()

    response = client.post(
        f"/api/v1/projects/{project_id}/trust",
        json={"confirmation": "trust"},
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "PROJECT_TRUST_INVALID"
    assert str(project) not in response.text


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
    assert "project_path" not in legacy_preview.json()
    diff = client.get(f"/api/v1/change-sets/{preview['change_set_id']}/diff")
    assert diff.status_code == 200
    assert "main.py" in diff.text
    backend = client.get("/api/v1/security/backends").json()["backends"][0]
    assert backend["verified"] is False
    assert backend["status"] == "unavailable"
    assert "skills" in client.get("/api/v1/skills").json()


def test_json_response_boundary_redacts_nested_credential_fields(
    tmp_path: Path, monkeypatch
) -> None:
    client, manager, _project = setup_app(tmp_path)

    def leaky(_change_set_id: str) -> dict[str, object]:
        return {
            "change_set_id": _change_set_id,
            "status": "verified",
            "metadata": {
                "accessKey": "nested-secret",
                "display_name": "safe-value",
            },
        }

    monkeypatch.setattr(manager, "get", leaky)
    response = client.get("/api/v1/change-sets/fixture-boundary")

    assert response.status_code == 200
    assert response.json()["metadata"] == {
        "accessKey": "[REDACTED]",
        "display_name": "safe-value",
    }
    assert "nested-secret" not in response.text


def test_json_response_boundary_redacts_nested_approval_token(
    tmp_path: Path, monkeypatch
) -> None:
    client, manager, _project = setup_app(tmp_path)

    monkeypatch.setattr(
        manager,
        "get",
        lambda change_set_id: {
            "change_set_id": change_set_id,
            "metadata": {"approval_token": "nested-capability"},
        },
    )
    response = client.get("/api/v1/change-sets/fixture-boundary")

    assert response.status_code == 200
    assert response.json()["metadata"]["approval_token"] == "[REDACTED]"
    assert "nested-capability" not in response.text


def test_approval_endpoint_preserves_opaque_token_for_operator(
    tmp_path: Path, monkeypatch
) -> None:
    client, manager, _project = setup_app(tmp_path)
    operator_token = "sk-operator-token-that-must-survive"
    monkeypatch.setattr(
        manager,
        "approve",
        lambda _change_set_id, _capability: {"approval_token": operator_token},
    )

    response = client.post("/api/v1/change-sets/operator/approve")

    assert response.status_code == 200
    assert response.json()["approval_token"] == operator_token


def test_delivery_report_preserves_boolean_capability_metadata(tmp_path: Path) -> None:
    client, _manager, project = setup_app(tmp_path)
    preview = make_preview(client, project)

    report = client.get(
        f"/api/v1/change-sets/{preview['change_set_id']}/delivery-report"
    )

    assert report.status_code == 200
    assert report.json()["rollback"]["requires_approval_token"] is True


def test_canonical_diff_redacts_credential_shaped_content(tmp_path: Path) -> None:
    client, _manager, project = setup_app(tmp_path)
    secret_content = (
        'api_key="my secret value"\n'
        'token="""triple " delimiter secret value"""\n'
    )
    response = client.post(
        "/api/v1/change-sets/preview",
        json={
            "project_path": str(project),
            "patches": [{
                "relative_path": "config.txt",
                "operation": "create",
                "content": secret_content,
            }],
        },
    )
    assert response.status_code == 201
    change_set_id = response.json()["change_set_id"]
    public_diff = client.get(f"/api/v1/change-sets/{change_set_id}/diff")
    assert "my secret value" not in public_diff.text
    assert "triple \" delimiter secret value" not in public_diff.text
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


def test_sse_cors_allows_last_event_id_replay_header(tmp_path: Path) -> None:
    client, manager, project = setup_app(tmp_path)
    preview = make_preview(client, project)
    run_id = manager.get_run_id(str(preview["change_set_id"]))
    assert run_id is not None

    response = client.options(
        f"/api/v1/runs/{run_id}/events",
        headers={
            "Origin": "http://127.0.0.1:18801",
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "Last-Event-ID",
        },
    )
    assert response.status_code == 200
    assert "Last-Event-ID" in response.headers["access-control-allow-headers"]


def test_idempotency_request_id_is_stable_across_app_instances(tmp_path: Path) -> None:
    client, _manager, project = setup_app(tmp_path)
    preview = make_preview(client, project)
    change_set_id = str(preview["change_set_id"])
    key = "stable-request-id"
    first = client.get(
        f"/api/v1/change-sets/{change_set_id}",
        headers={"Idempotency-Key": key},
    )
    assert first.status_code == 200

    restarted_store = ProjectStore(tmp_path / "data" / "devpilot.db")
    restarted_manager = ControlPlaneManager(
        allowed_roots=AllowedRoots.from_paths([tmp_path]), store=restarted_store
    )
    restarted = TestClient(
        create_app(manager=restarted_manager), client=("127.0.0.1", 50001)
    ).get(
        f"/api/v1/change-sets/{change_set_id}",
        headers={"Idempotency-Key": key},
    )
    assert restarted.status_code == 200
    assert restarted.headers["X-Request-ID"] == first.headers["X-Request-ID"]
