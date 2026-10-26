import csv
import io
import os
import sys
import tempfile
import uuid
from urllib.parse import parse_qs, urlparse
from pathlib import Path

# Pytest's import mode can omit the repository root from sys.path.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Ensure the test import never targets a production database.
_TEST_DB = os.path.join(tempfile.gettempdir(), "lifetrade-test-" + uuid.uuid4().hex + ".sqlite3")
os.environ["DATABASE_URL"] = ""
os.environ["SQLITE_PATH"] = _TEST_DB

import pytest
from fastapi.testclient import TestClient

import barsan_experiment as main


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DB_PATH", str(tmp_path / "lifetrade.sqlite3"))
    main.init_db()
    with TestClient(main.app) as test_client:
        yield test_client


def create_session(client, participant_code="TEST-P001", experiment_version="test"):
    response = client.post(
        "/api/session",
        json={
            "participant_code": participant_code,
            "experiment_version": experiment_version,
            "user_agent": "pytest",
            "screen_width": 390,
            "screen_height": 844,
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["participant_code"] == participant_code.strip().upper()
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
            "SELECT trial_count FROM instrument_runs WHERE run_id = ?",
            (session_id,),
        ).fetchone()
        total = connection.execute(
            "SELECT COUNT(*) AS n FROM lifetrade_trials WHERE run_id = ?",
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
            "SELECT trial_count FROM instrument_runs WHERE run_id = ?",
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
            "SELECT status FROM instrument_runs WHERE run_id = ?",
            (session_id,),
        ).fetchone()["status"]
    assert status == "completed"


def test_admin_csv_export_streams_trial_rows(client, monkeypatch):
    monkeypatch.setattr(main, "ADMIN_TOKEN", "test-token")
    session_id = create_session(client)
    saved = client.post(
        "/api/trials",
        json={
            "session_id": session_id,
            "trials": [
                {"client_event_id": "export-event-1", "row": {"index": 1, "choice": "Now"}},
                {"client_event_id": "export-event-2", "row": {"index": 2, "choice": "Later"}},
            ],
        },
    )
    assert saved.status_code == 200

    response = client.get("/api/admin/export.csv", headers={"x-admin-token": "test-token"})
    assert response.status_code == 200
    rows = list(csv.DictReader(io.StringIO(response.text)))
    assert len(rows) == 2
    assert {row["index"] for row in rows} == {"1", "2"}
    assert {row["choice"] for row in rows} == {"Now", "Later"}
    assert all(row["server_session_id"] == session_id for row in rows)
    assert all(row["server_run_id"] == session_id for row in rows)
    assert all(row["participant_code"] == "TEST-P001" for row in rows)
    assert all(row["instrument_code"] == "lifetrade" for row in rows)
    assert all(row["server_received_at"] for row in rows)


def test_supplied_participant_code_is_reused_across_runs(client):
    first_run = create_session(client, participant_code="event-0042", experiment_version="v1")
    second_run = create_session(client, participant_code="EVENT-0042", experiment_version="v1")

    with main.conn() as connection:
        participant_count = connection.execute(
            "SELECT COUNT(*) AS n FROM participants WHERE participant_code = ?",
            ("EVENT-0042",),
        ).fetchone()["n"]
        participation_count = connection.execute(
            "SELECT COUNT(*) AS n FROM study_participations sp "
            "JOIN participants p ON p.participant_id=sp.participant_id "
            "WHERE p.participant_code = ?",
            ("EVENT-0042",),
        ).fetchone()["n"]
        run_count = connection.execute(
            "SELECT COUNT(*) AS n FROM instrument_runs WHERE run_id IN (?,?)",
            (first_run, second_run),
        ).fetchone()["n"]

    assert participant_count == 1
    assert participation_count == 1
    assert run_count == 2


def test_participant_code_is_required_and_not_generated(client):
    response = client.post(
        "/api/session",
        json={"experiment_version": "test"},
    )
    assert response.status_code == 422

    with main.conn() as connection:
        count = connection.execute(
            "SELECT COUNT(*) AS n FROM participants"
        ).fetchone()["n"]
    assert count == 0


def _create_assignment(client, monkeypatch, code="BS-0001", condition="A"):
    monkeypatch.setattr(main, "ADMIN_TOKEN", "test-admin-token")
    response = client.post(
        "/api/admin/assignments",
        headers={"x-admin-token": "test-admin-token"},
        json={"assignments": [{"participant_code": code, "condition_code": condition}]},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["created_count"] == 1
    assert payload["assignments"][0]["condition_code"] == condition
    token = parse_qs(urlparse(payload["assignments"][0]["invitation_url"]).query)["invite"][0]
    return token


def test_assignment_identity_and_wrong_code_rejected(client, monkeypatch):
    invitation_token = _create_assignment(client, monkeypatch, code="BS-0001", condition="B")
    identified = client.post(
        "/api/experiment/identify",
        json={"invitation_token": invitation_token, "participant_code": " bs-0001 "},
    )
    assert identified.status_code == 200, identified.text
    payload = identified.json()
    assert payload["condition_code"] == "B"
    assert payload["current_stage"] == "questionnaire"
    assert [stage["status"] for stage in payload["stages"]] == [
        "not_started", "not_started", "not_started"
    ]

    wrong_code = client.post(
        "/api/experiment/identify",
        json={"invitation_token": invitation_token, "participant_code": "BS-9999"},
    )
    assert wrong_code.status_code == 403


def test_stage_order_draft_resume_and_task_restart(client, monkeypatch):
    invitation_token = _create_assignment(client, monkeypatch, code="BS-0002", condition="A")
    identity = {"invitation_token": invitation_token, "participant_code": "BS-0002"}

    blocked = client.post(
        "/api/experiment/stages/start",
        json={**identity, "stage_code": "dd_lifetrade"},
    )
    assert blocked.status_code == 409

    questionnaire = client.post(
        "/api/experiment/stages/start",
        json={**identity, "stage_code": "questionnaire"},
    )
    assert questionnaire.status_code == 200, questionnaire.text
    q = questionnaire.json()
    assert q["attempt_number"] == 1
    qrun = q["run_id"]

    draft = {"age_range": "25-34", "answers": {"B1": "2"}, "current_question_index": 0}
    saved = client.post(
        "/api/experiment/stages/draft",
        json={**identity, "run_id": qrun, "draft_payload": draft},
    )
    assert saved.status_code == 200, saved.text

    refreshed = client.post("/api/experiment/identify", json=identity)
    assert refreshed.status_code == 200
    qstate = refreshed.json()["stages"][0]
    assert qstate["status"] == "in_progress"
    assert qstate["current_run_id"] == qrun
    assert qstate["draft_payload"] == draft

    resumed = client.post(
        "/api/experiment/stages/start",
        json={**identity, "stage_code": "questionnaire"},
    )
    assert resumed.status_code == 200
    assert resumed.json()["resumed"] is True
    assert resumed.json()["run_id"] == qrun
    assert resumed.json()["draft_payload"] == draft

    # Save each of the configured 25 questions through the per-code endpoint.
    final_answers = {}
    for question_code in main.QUESTIONNAIRE_QUESTION_CODES:
        answer = client.post(
            "/api/experiment/questionnaire/answer",
            json={**identity, "run_id": qrun, "question_code": question_code,
                  "response_value": "3", "current_question_index": 0},
        )
        assert answer.status_code == 200, answer.text
        final_answers[question_code] = "3"

    completed_questionnaire = client.post(
        "/api/experiment/stages/complete",
        json={**identity, "stage_code": "questionnaire", "run_id": qrun,
              "final_payload": {"answers": final_answers}},
    )
    assert completed_questionnaire.status_code == 200, completed_questionnaire.text

    first_task = client.post(
        "/api/experiment/stages/start",
        json={**identity, "stage_code": "dd_lifetrade"},
    )
    assert first_task.status_code == 200, first_task.text
    old_run = first_task.json()["run_id"]
    assert first_task.json()["attempt_number"] == 1

    saved_trial = client.post(
        "/api/trials",
        json={"session_id": old_run,
              "trials": [{"client_event_id": "old-dd-event", "row": {"index": 1, "choice": "Now"}}]},
    )
    assert saved_trial.status_code == 200, saved_trial.text

    restarted_task = client.post(
        "/api/experiment/stages/start",
        json={**identity, "stage_code": "dd_lifetrade"},
    )
    assert restarted_task.status_code == 200, restarted_task.text
    new_run = restarted_task.json()["run_id"]
    assert new_run != old_run
    assert restarted_task.json()["attempt_number"] == 2
    assert restarted_task.json()["draft_payload"] == {}

    with main.conn() as connection:
        old_status = connection.execute(
            "SELECT status FROM instrument_runs WHERE run_id=?", (old_run,)
        ).fetchone()["status"]
    assert old_status == "superseded"

    # Superseded partial task data stay auditable in the database but are omitted from the primary export.
    monkeypatch.setattr(main, "ADMIN_TOKEN", "test-admin-token")
    exported = client.get("/api/admin/export.csv", headers={"x-admin-token": "test-admin-token"})
    assert exported.status_code == 200, exported.text
    rows = list(csv.DictReader(io.StringIO(exported.text)))
    assert all(row.get("index") != "1" for row in rows)
    assert "old-dd-event" not in exported.text

    completed_dd = client.post(
        "/api/experiment/stages/complete",
        json={**identity, "stage_code": "dd_lifetrade", "run_id": new_run},
    )
    assert completed_dd.status_code == 200, completed_dd.text

    ultimatum = client.post(
        "/api/experiment/stages/start",
        json={**identity, "stage_code": "ultimatum"},
    )
    assert ultimatum.status_code == 200, ultimatum.text
    assert ultimatum.json()["attempt_number"] == 1


def test_questionnaire_answer_is_upserted_by_hidden_question_code(client, monkeypatch):
    token = _create_assignment(client, monkeypatch, code="BS-Q-0001", condition="B")
    identity = {"invitation_token": token, "participant_code": "BS-Q-0001"}
    started = client.post(
        "/api/experiment/stages/start",
        json={**identity, "stage_code": "questionnaire"},
    )
    assert started.status_code == 200, started.text
    run_id = started.json()["run_id"]

    first = client.post(
        "/api/experiment/questionnaire/answer",
        json={**identity, "run_id": run_id, "question_code": "PJ1",
              "response_value": "2", "current_question_index": 5},
    )
    assert first.status_code == 200, first.text
    second = client.post(
        "/api/experiment/questionnaire/answer",
        json={**identity, "run_id": run_id, "question_code": "PJ1",
              "response_value": "4", "current_question_index": 5},
    )
    assert second.status_code == 200, second.text

    unknown = client.post(
        "/api/experiment/questionnaire/answer",
        json={**identity, "run_id": run_id, "question_code": "NOT_A_CODE",
              "response_value": "4", "current_question_index": 5},
    )
    assert unknown.status_code == 422

    with main.conn() as connection:
        rows = connection.execute(
            "SELECT question_code,response_value FROM questionnaire_responses WHERE run_id=?",
            (run_id,),
        ).fetchall()
        record = connection.execute(
            "SELECT draft_payload FROM experiment_stage_progress WHERE current_run_id=?",
            (run_id,),
        ).fetchone()
    assert len(rows) == 1
    assert rows[0]["question_code"] == "PJ1"
    assert rows[0]["response_value"] == "4"
    import json
    draft_payload = record["draft_payload"] if isinstance(record["draft_payload"], dict) else json.loads(record["draft_payload"])
    assert draft_payload["answers"]["PJ1"] == "4"


def test_admin_progress_and_questionnaire_csv(client, monkeypatch):
    token = _create_assignment(client, monkeypatch, code="BS-PROGRESS-01", condition="B")
    identity = {"invitation_token": token, "participant_code": "BS-PROGRESS-01"}
    started = client.post(
        "/api/experiment/stages/start",
        json={**identity, "stage_code": "questionnaire"},
    )
    assert started.status_code == 200, started.text
    run_id = started.json()["run_id"]

    answers = {}
    for question_code in main.QUESTIONNAIRE_QUESTION_CODES:
        response = client.post(
            "/api/experiment/questionnaire/answer",
            json={**identity, "run_id": run_id, "question_code": question_code,
                  "response_value": "5", "current_question_index": 24},
        )
        assert response.status_code == 200, response.text
        answers[question_code] = "5"
    completed = client.post(
        "/api/experiment/stages/complete",
        json={**identity, "stage_code": "questionnaire", "run_id": run_id,
              "final_payload": {"answers": answers}},
    )
    assert completed.status_code == 200, completed.text

    monkeypatch.setattr(main, "ADMIN_TOKEN", "admin-export-token")
    headers = {"x-admin-token": "admin-export-token"}
    progress = client.get("/api/admin/progress", headers=headers)
    assert progress.status_code == 200, progress.text
    participant = next(row for row in progress.json() if row["participant_code"] == "BS-PROGRESS-01")
    assert participant["condition_code"] == "B"
    assert participant["questionnaire_status"] == "completed"
    assert participant["dd_lifetrade_status"] == "not_started"
    assert participant["ultimatum_status"] == "not_started"

    exported = client.get("/api/admin/questionnaire/export.csv", headers=headers)
    assert exported.status_code == 200, exported.text
    rows = list(csv.DictReader(io.StringIO(exported.text.lstrip("\\ufeff"))))
    assert len(rows) == len(main.QUESTIONNAIRE_QUESTION_CODES)
    assert {row["question_code"] for row in rows} == set(main.QUESTIONNAIRE_QUESTION_CODES)
    assert all(row["participant_code"] == "BS-PROGRESS-01" for row in rows)
    assert all(row["condition_code"] == "B" and row["response_value"] == "5" for row in rows)


def test_assignment_creation_requires_admin_and_does_not_allow_reassignment(client, monkeypatch):
    denied = client.post(
        "/api/admin/assignments",
        json={"assignments": [{"participant_code": "BS-0003", "condition_code": "A"}]},
    )
    assert denied.status_code == 401

    _create_assignment(client, monkeypatch, code="BS-0003", condition="A")
    duplicate = client.post(
        "/api/admin/assignments",
        headers={"x-admin-token": "test-admin-token"},
        json={"assignments": [{"participant_code": "BS-0003", "condition_code": "B"}]},
    )
    assert duplicate.status_code == 409
