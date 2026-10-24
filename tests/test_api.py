import os
import tempfile
import uuid

# Ensure the test import never targets a production database.
_TEST_DB = os.path.join(tempfile.gettempdir(), "lifetrade-test-" + uuid.uuid4().hex + ".sqlite3")
os.environ["DATABASE_URL"] = ""
os.environ["SQLITE_PATH"] = _TEST_DB

import pytest
from fastapi.testclient import TestClient

import main


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DB_PATH", str(tmp_path / "lifetrade.sqlite3"))
    main.init_db()
    with TestClient(main.app) as test_client:
        yield test_client


def create_session(client):
    response = client.post(
        "/api/session",
        json={
            "experiment_version": "test",
            "user_agent": "pytest",
            "screen_width": 390,
            "screen_height": 844,
        },
    )
    assert response.status_code == 200
    return response.json()["session_id"]


def test_health_reports_database_connection(client):
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert response.json()["database_connected"] is True


def test_batch_trial_insert_is_idempotent_and_counts_only_new_events(client):
    session_id = create_session(client)
    payload = {
        "session_id": session_id,
        "trials": [
            {"client_event_id": "event-1", "row": {"index": 1, "choice": "Now"}},
            {"client_event_id": "event-2", "row": {"index": 2, "choice": "Later"}},
        ],
    }

    first = client.post("/api/trials", json=payload)
    assert first.status_code == 200
    assert first.json()["inserted_count"] == 2
    assert first.json()["duplicate_count"] == 0

    duplicate = client.post("/api/trials", json=payload)
    assert duplicate.status_code == 200
    assert duplicate.json()["inserted_count"] == 0
    assert duplicate.json()["duplicate_count"] == 2

    with main.conn() as connection:
        row = connection.execute(
            "SELECT trial_count FROM sessions WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        total = connection.execute(
            "SELECT COUNT(*) AS n FROM trials WHERE session_id = ?",
            (session_id,),
        ).fetchone()["n"]

    assert row["trial_count"] == 2
    assert total == 2


def test_single_trial_endpoint_remains_compatible(client):
    session_id = create_session(client)
    response = client.post(
        "/api/trial",
        json={
            "session_id": session_id,
            "client_event_id": "legacy-event-1",
            "row": {"index": 1, "choice": "Now"},
        },
    )
    assert response.status_code == 200
    assert response.json() == {"saved": True, "client_event_id": "legacy-event-1"}

    with main.conn() as connection:
        count = connection.execute(
            "SELECT trial_count FROM sessions WHERE session_id = ?",
            (session_id,),
        ).fetchone()["trial_count"]
    assert count == 1


def test_unknown_session_is_rejected(client):
    response = client.post(
        "/api/trials",
        json={
            "session_id": "not-a-real-session",
            "trials": [{"client_event_id": "event-1", "row": {"index": 1}}],
        },
    )
    assert response.status_code == 404


def test_complete_marks_existing_session(client):
    session_id = create_session(client)
    response = client.post(
        "/api/complete",
        json={"session_id": session_id, "status": "completed", "trial_count": 0},
    )
    assert response.status_code == 200
    assert response.json() == {"saved": True, "status": "completed"}

    with main.conn() as connection:
        status = connection.execute(
            "SELECT status FROM sessions WHERE session_id = ?",
            (session_id,),
        ).fetchone()["status"]
    assert status == "completed"
