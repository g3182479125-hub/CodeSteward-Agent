from __future__ import annotations

import asyncio
import json
from typing import Any

import devpilot.nanobot_tools as nanobot_tools
from devpilot.event_bridge import project_run_state, project_sse_events
from devpilot.nanobot_tools import ToolResult, WatchRunTool


def test_run_projection_is_allowlisted_and_deterministic() -> None:
    projection = project_run_state(
        {
            "run_id": "run-1",
            "status": "completed",
            "project_path": r"D:\secret\repo",
            "events": [
                {
                    "event_id": 1,
                    "sequence": 1,
                    "event_type": "run.started",
                    "created_at": "2026-09-04T12:00:00+00:00",
                    "payload": {
                        "backend": "fake",
                        "stdout": "password=not-for-model",
                        "project_path": r"D:\secret\repo",
                    },
                },
                {
                    "event_id": 2,
                    "sequence": 2,
                    "event_type": "not-a-public-event",
                    "created_at": "now",
                    "payload": {"code": "SHOULD_NOT_APPEAR"},
                },
                {
                    "event_id": 3,
                    "sequence": 3,
                    "event_type": "run.completed",
                    "created_at": "2026-09-04T12:00:01+00:00",
                    "payload": {"status": "completed", "files": ["a.py", "b.py"]},
                },
            ],
        }
    )

    rendered = projection.to_dict()
    assert rendered["run_id"] == "run-1"
    assert rendered["last_event_id"] == 3
    assert [event["event_id"] for event in rendered["events"]] == [1, 3]
    text = json.dumps(rendered, ensure_ascii=False)
    assert "secret" not in text
    assert "password" not in text
    assert "stdout" not in text
    assert "SHOULD_NOT_APPEAR" not in text
    assert rendered["events"][1]["details"] == {"file_count": 2, "status": "completed"}


def test_sse_projection_preserves_replay_order_and_hides_raw_output() -> None:
    projection = project_sse_events(
        run_id="run-2",
        status="running",
        events=[
            {
                "event_id": 7,
                "run_id": "run-2",
                "sequence": 4,
                "type": "execution.capability_denied",
                "created_at": "now",
                "redacted_payload": {"code": "EXECUTION_BACKEND_UNAVAILABLE", "stderr": "secret"},
            },
            {
                "event_id": 8,
                "run_id": "run-2",
                "sequence": 5,
                "type": "run.failed",
                "created_at": "now",
                "redacted_payload": {"code": "EXECUTION_BACKEND_UNAVAILABLE"},
            },
        ]
    )
    assert [item.event_id for item in projection.events] == [7, 8]
    assert projection.status == "running"
    assert all("stderr" not in json.dumps(item.to_dict()) for item in projection.events)


def test_projection_rejects_unsafe_scalar_details_and_keeps_preview_events() -> None:
    projection = project_run_state(
        {
            "run_id": "run-safe",
            "status": "running",
            "events": [
                {
                    "event_id": 1,
                    "sequence": 1,
                    "event_type": "changeset.previewed",
                    "created_at": "now",
                    "payload": {
                        "code": r"D:\private\secret.txt",
                        "artifact_ref": r"D:\private\artifact.md",
                        "capability": "sk-1234567890-secret-token",
                        "backend": "fake",
                        "files": r"D:\private\other.txt",
                    },
                }
            ],
        }
    )

    assert len(projection.events) == 1
    details = projection.events[0].details
    assert details == {"backend": "fake"}


def test_projection_rejects_unsafe_run_identity_and_status() -> None:
    try:
        project_run_state({"run_id": r"D:\private\secret", "status": "completed"})
    except ValueError as exc:
        assert str(exc) == "run_id is required"
    else:
        raise AssertionError("unsafe run identity must be rejected")

    projection = project_run_state({"run_id": "run-safe", "status": r"D:\private\secret"})
    assert projection.status == "unknown"


def test_sse_frame_metadata_wins_over_conflicting_payload_metadata(monkeypatch: Any) -> None:
    def fake_urlopen(_request: Any, timeout: int) -> _Response:
        assert timeout == 15
        return _Response(
            b"id: 7\n"
            b"event: changeset.previewed\n"
            b'data: {"event_id": 999,"run_id":"run-5","sequence":99,"type":"run.failed",'
            b'"created_at":"now","redacted_payload":{}}\n\n'
        )

    monkeypatch.setattr(nanobot_tools, "urlopen", fake_urlopen)
    run_id, _status, events = nanobot_tools._get_control_plane_events("/v1/runs/run-5/events")

    assert run_id == "run-5"
    assert events[0]["event_id"] == 7
    assert events[0]["type"] == "changeset.previewed"


def test_sse_parser_rejects_non_monotonic_ids_and_sequences(monkeypatch: Any) -> None:
    def fake_urlopen(_request: Any, timeout: int) -> _Response:
        assert timeout == 15
        return _Response(
            b"id: 7\n"
            b"event: run.started\n"
            b'data: {"run_id":"run-6","sequence":2}\n\n'
            b"id: 7\n"
            b"event: run.completed\n"
            b'data: {"run_id":"run-6","sequence":3}\n\n'
        )

    monkeypatch.setattr(nanobot_tools, "urlopen", fake_urlopen)
    try:
        nanobot_tools._get_control_plane_events("/v1/runs/run-6/events")
    except ValueError as exc:
        assert str(exc) == "control-plane SSE event order is invalid"
    else:
        raise AssertionError("non-monotonic SSE IDs must be rejected")


class _Response:
    def __init__(self, body: bytes) -> None:
        self.body = body

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_args: Any) -> None:
        return None

    def read(self, _limit: int = -1) -> bytes:
        return self.body


def test_watch_run_reads_sse_and_supports_last_event_id(monkeypatch: Any) -> None:
    observed: dict[str, str] = {}

    def fake_urlopen(request: Any, timeout: int) -> _Response:
        observed["url"] = request.full_url
        observed["last_event_id"] = request.get_header("Last-event-id") or ""
        body = (
            b"id: 10\n"
            b"event: changeset.applied\n"
            b'data: {"event_id":10,"run_id":"run-3","sequence":2,"type":"changeset.applied",'
            b'"created_at":"now","redacted_payload":{"files":1}}\n\n'
        )
        return _Response(body)

    monkeypatch.setattr(nanobot_tools, "urlopen", fake_urlopen)
    raw = asyncio.run(WatchRunTool().execute(run_id="run-3", last_event_id=9))
    assert not isinstance(raw, ToolResult)
    payload = json.loads(str(raw))
    assert payload["kind"] == "devpilot.run"
    assert payload["events"][0]["message"] == "ChangeSet 已应用，等待验证或交付决策。"
    assert observed["url"].endswith("/v1/runs/run-3/events")
    assert observed["last_event_id"] == "9"


def test_watch_run_falls_back_to_snapshot_on_sse_transport_failure(monkeypatch: Any) -> None:
    calls: list[str] = []

    def fake_events(*_args: Any, **_kwargs: Any) -> Any:
        calls.append("events")
        raise OSError("connection reset")

    def fake_snapshot(path: str) -> dict[str, Any]:
        calls.append(path)
        return {
            "run_id": "run-4",
            "status": "failed",
            "events": [
                {
                    "event_id": 1,
                    "sequence": 1,
                    "event_type": "run.failed",
                    "created_at": "now",
                    "payload": {"code": "EXECUTION_BACKEND_UNAVAILABLE"},
                }
            ],
        }

    monkeypatch.setattr(nanobot_tools, "_get_control_plane_events", fake_events)
    monkeypatch.setattr(nanobot_tools, "_get_control_plane", fake_snapshot)
    raw = asyncio.run(WatchRunTool().execute(run_id="run-4"))
    assert not isinstance(raw, ToolResult)
    payload = json.loads(str(raw))
    assert payload["fallback"] is True
    assert payload["status"] == "failed"
    assert calls == ["events", "/v1/runs/run-4"]


def test_watch_run_falls_back_to_snapshot_when_sse_has_no_new_events(monkeypatch: Any) -> None:
    calls: list[str] = []

    def fake_events(*_args: Any, **_kwargs: Any) -> Any:
        calls.append("events")
        return "", "running", []

    def fake_snapshot(path: str) -> dict[str, Any]:
        calls.append(path)
        return {
            "run_id": "run-5",
            "status": "completed",
            "events": [
                {
                    "event_id": 4,
                    "sequence": 4,
                    "event_type": "run.completed",
                    "created_at": "now",
                    "payload": {},
                }
            ],
        }

    monkeypatch.setattr(nanobot_tools, "_get_control_plane_events", fake_events)
    monkeypatch.setattr(nanobot_tools, "_get_control_plane", fake_snapshot)
    raw = asyncio.run(WatchRunTool().execute(run_id="run-5", last_event_id=3))

    assert not isinstance(raw, ToolResult)
    payload = json.loads(str(raw))
    assert payload["fallback"] is True
    assert payload["status"] == "completed"
    assert calls == ["events", "/v1/runs/run-5"]
