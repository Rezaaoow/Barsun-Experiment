import csv
import io
import os
import sys
import tempfile
import uuid
import zipfile
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
    consent = client.post(
        "/api/experiment/consent",
        json={"invitation_token": token, "participant_code": code, "accepted": True},
    )
    assert consent.status_code == 200, consent.text
    return token


def test_identity_requires_persisted_consent(client, monkeypatch):
    monkeypatch.setattr(main, "ADMIN_TOKEN", "test-admin-token")
    response = client.post(
        "/api/admin/assignments",
        headers={"x-admin-token": "test-admin-token"},
        json={"assignments": [{"participant_code": "BS-CONSENT-01", "condition_code": "A"}]},
    )
    assert response.status_code == 200, response.text
    invitation = response.json()["assignments"][0]["invitation_url"]
    token = parse_qs(urlparse(invitation).query)["invite"][0]
    identity = {"invitation_token": token, "participant_code": "BS-CONSENT-01"}

    not_yet_consented = client.post("/api/experiment/identify", json=identity)
    assert not_yet_consented.status_code == 403

    rejected = client.post("/api/experiment/consent", json={**identity, "accepted": False})
    assert rejected.status_code == 403
    still_not_consented = client.post("/api/experiment/identify", json=identity)
    assert still_not_consented.status_code == 403

    accepted = client.post("/api/experiment/consent", json={**identity, "accepted": True})
    assert accepted.status_code == 200, accepted.text
    identified = client.post("/api/experiment/identify", json=identity)
    assert identified.status_code == 200, identified.text
    assert accepted.json()["consent_version"] == "barsan-consent-v1"


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
    rows = list(csv.DictReader(io.StringIO(exported.text.lstrip("\ufeff"))))
    assert len(rows) == len(main.QUESTIONNAIRE_QUESTION_CODES)
    assert {row["question_code"] for row in rows} == set(main.QUESTIONNAIRE_QUESTION_CODES)
    assert all(row["participant_code"] == "BS-PROGRESS-01" for row in rows)
    assert all(row["condition_code"] == "B" and row["response_value"] == "5" for row in rows)


def test_admin_test_scope_monitoring_and_exports(client, monkeypatch):
    # Create a main-study assignment and isolated test studies; each scope must stay separate.
    _create_assignment(client, monkeypatch, code="BS-MAIN-SCOPE-01", condition="A")
    monkeypatch.setattr(main, "ADMIN_TOKEN", "test-scope-admin-token")
    headers = {"x-admin-token": "test-scope-admin-token"}

    created = client.post("/api/admin/test-links", headers=headers)
    assert created.status_code == 200, created.text

    test_progress = client.get("/api/admin/progress?scope=test", headers=headers)
    assert test_progress.status_code == 200, test_progress.text
    test_rows = test_progress.json()
    assert len(test_rows) == 2
    assert all(row["study_code"].startswith("barsan-test-") for row in test_rows)
    assert {row["participant_code"] for row in test_rows} == {"TEST"}

    main_progress = client.get("/api/admin/progress?scope=main", headers=headers)
    assert main_progress.status_code == 200, main_progress.text
    assert any(row["participant_code"] == "BS-MAIN-SCOPE-01" for row in main_progress.json())
    assert all(row["participant_code"] != "TEST" for row in main_progress.json())

    preview = client.get("/api/admin/maintenance/preview?scope=test", headers=headers)
    assert preview.status_code == 200, preview.text
    assert preview.json()["counts"]["assignments"] == 2

    for path in (
        "/api/admin/questionnaire/export.csv?scope=test",
        "/api/admin/export.csv?scope=test",
        "/api/admin/ultimatum/export.csv?scope=test",
    ):
        response = client.get(path, headers=headers)
        assert response.status_code == 200, f"{path}: {response.text}"
        assert response.headers.get("content-type", "").startswith("text/csv")

    archive_response = client.get("/api/admin/export-all.zip?scope=test", headers=headers)
    assert archive_response.status_code == 200, archive_response.text
    with zipfile.ZipFile(io.BytesIO(archive_response.content)) as archive:
        assert {"questionnaire.csv", "lifetrade.csv", "ultimatum.csv", "participant_progress.csv", "README.txt"} <= set(archive.namelist())


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


def _start_ultimatum_run(client, monkeypatch, code="BS-ULT-001", condition="A"):
    token = _create_assignment(client, monkeypatch, code=code, condition=condition)
    identity = {"invitation_token": token, "participant_code": code}

    questionnaire = client.post(
        "/api/experiment/stages/start",
        json={**identity, "stage_code": "questionnaire"},
    )
    assert questionnaire.status_code == 200, questionnaire.text
    questionnaire_run = questionnaire.json()["run_id"]
    answers = {}
    for index, question_code in enumerate(main.QUESTIONNAIRE_QUESTION_CODES):
        answer = client.post(
            "/api/experiment/questionnaire/answer",
            json={
                **identity,
                "run_id": questionnaire_run,
                "question_code": question_code,
                "response_value": "3",
                "current_question_index": index,
            },
        )
        assert answer.status_code == 200, answer.text
        answers[question_code] = "3"
    completed_questionnaire = client.post(
        "/api/experiment/stages/complete",
        json={
            **identity,
            "stage_code": "questionnaire",
            "run_id": questionnaire_run,
            "final_payload": {"answers": answers},
        },
    )
    assert completed_questionnaire.status_code == 200, completed_questionnaire.text

    dd = client.post(
        "/api/experiment/stages/start",
        json={**identity, "stage_code": "dd_lifetrade"},
    )
    assert dd.status_code == 200, dd.text
    completed_dd = client.post(
        "/api/experiment/stages/complete",
        json={
            **identity,
            "stage_code": "dd_lifetrade",
            "run_id": dd.json()["run_id"],
        },
    )
    assert completed_dd.status_code == 200, completed_dd.text

    ultimatum = client.post(
        "/api/experiment/stages/start",
        json={**identity, "stage_code": "ultimatum"},
    )
    assert ultimatum.status_code == 200, ultimatum.text
    return identity, ultimatum.json()


def _resolve_ultimatum_round(client, identity, run_id, round_id, offer=None, decision=None):
    payload = {
        **identity,
        "run_id": run_id,
        "round_id": round_id,
        "decision_screen_shown_at": main.now(),
        "responded_at": main.now(),
        "reaction_time_ms": 123.45,
        "wait_duration_ms": 2000,
        "client_metadata": {
            "user_agent": "pytest",
            "screen_width": 390,
            "screen_height": 844,
        },
    }
    if offer is not None:
        payload["participant_offer_to_opponent"] = offer
    else:
        payload["participant_decision"] = decision
    response = client.post("/api/experiment/ultimatum/resolve", json=payload)
    assert response.status_code == 200, response.text
    return response.json()


def test_ultimatum_ai_models_are_bounded_and_auditable(monkeypatch):
    monkeypatch.setattr(main.random, "choices", lambda values, weights, k: [4])
    participant_share, offer_model = main._sample_ai_offer_to_participant()
    assert participant_share == 6
    assert offer_model["model"] == "discrete_truncated_normal"
    assert offer_model["sampled_ai_self_share"] == 4
    assert offer_model["sampled_participant_share"] == 6
    assert offer_model["support"] == list(range(11))
    assert offer_model["mean"] == 5.0
    assert offer_model["sd"] == 2.0

    # Six successes out of ten at p=.6 cross the acceptance threshold.
    draws = iter([0.1] * 6 + [0.9] * 4)
    monkeypatch.setattr(main.random, "random", lambda: next(draws))
    accepted = main._sample_ai_response(6)
    assert accepted["model"] == "binomial_threshold"
    assert accepted["n"] == 10
    assert accepted["p"] == 0.6
    assert accepted["draw"] == 6
    assert accepted["decision"] == "accept"

    # Four successes out of ten at p=.4 do not cross the threshold.
    draws = iter([0.1] * 4 + [0.9] * 6)
    monkeypatch.setattr(main.random, "random", lambda: next(draws))
    rejected = main._sample_ai_response(4)
    assert rejected["p"] == 0.4
    assert rejected["draw"] == 4
    assert rejected["decision"] == "reject"


def test_ultimatum_blocks_are_server_randomized_and_order_enforced(client, monkeypatch):
    identity, started = _start_ultimatum_run(
        client, monkeypatch, code="BS-ULT-ORDER-01", condition="A"
    )
    run_id = started["run_id"]
    block_order = started["draft_payload"]["block_order"]
    assert len(block_order) == main.ULTIMATUM_MAIN_BLOCK_COUNT
    assert len(set(block_order)) == main.ULTIMATUM_MAIN_BLOCK_COUNT
    assert set(block_order) == set(main.ULTIMATUM_BLOCKS)

    with main.conn() as connection:
        run = connection.execute(
            "SELECT metadata FROM instrument_runs WHERE run_id=?", (run_id,)
        ).fetchone()
    metadata = main._json_value(run["metadata"])
    assert metadata["block_order"] == block_order

    # The server will not allow a participant to skip the practice phase.
    blocked_main = client.post(
        "/api/experiment/ultimatum/prepare",
        json={
            **identity, "run_id": run_id, "phase": "main",
            "block_code": block_order[0], "block_order": 1,
            "trial_in_block": 1, "trial_sequence": 1,
        },
    )
    assert blocked_main.status_code == 409

    # Nor can the responder practice be started before the proposer practice is completed.
    practice_proposer = client.post(
        "/api/experiment/ultimatum/prepare",
        json={
            **identity, "run_id": run_id, "phase": "practice",
            "block_code": "practice_proposer_agent", "block_order": 0,
            "trial_in_block": 1, "trial_sequence": 0,
        },
    )
    assert practice_proposer.status_code == 200, practice_proposer.text
    premature_responder = client.post(
        "/api/experiment/ultimatum/prepare",
        json={
            **identity, "run_id": run_id, "phase": "practice",
            "block_code": "practice_responder_coworker", "block_order": 0,
            "trial_in_block": 1, "trial_sequence": 0,
        },
    )
    assert premature_responder.status_code == 409

    resolved_practice_proposer = _resolve_ultimatum_round(
        client, identity, run_id, practice_proposer.json()["round_id"], offer=4
    )
    assert resolved_practice_proposer["phase"] == "practice"
    assert resolved_practice_proposer["total_score"] == 0
    assert resolved_practice_proposer["total_potential_lost"] == 0

    practice_responder = client.post(
        "/api/experiment/ultimatum/prepare",
        json={
            **identity, "run_id": run_id, "phase": "practice",
            "block_code": "practice_responder_coworker", "block_order": 0,
            "trial_in_block": 1, "trial_sequence": 0,
        },
    )
    assert practice_responder.status_code == 200, practice_responder.text
    resolved_practice_responder = _resolve_ultimatum_round(
        client, identity, run_id, practice_responder.json()["round_id"], decision="accept"
    )
    assert resolved_practice_responder["phase"] == "practice"
    assert resolved_practice_responder["total_score"] == 0

    # The URL/client cannot replace the randomized block order saved at stage start.
    wrong_code = next(code for code in main.ULTIMATUM_BLOCKS if code != block_order[0])
    wrong_order = client.post(
        "/api/experiment/ultimatum/prepare",
        json={
            **identity, "run_id": run_id, "phase": "main",
            "block_code": wrong_code, "block_order": 1,
            "trial_in_block": 1, "trial_sequence": 1,
        },
    )
    assert wrong_order.status_code == 409


def test_ultimatum_complete_run_scores_and_csv_export(client, monkeypatch):
    # Keep the first block proposer/agent so the rejection edge case is deterministic.
    monkeypatch.setattr(
        main.random, "sample",
        lambda population, k: ["proposer_agent", "responder_agent", "proposer_coworker", "responder_coworker"],
    )
    identity, started = _start_ultimatum_run(
        client, monkeypatch, code="BS-ULT-COMPLETE-01", condition="A"
    )
    run_id = started["run_id"]
    block_order = started["draft_payload"]["block_order"]

    first_block = block_order[0]
    first_round = client.post(
        "/api/experiment/ultimatum/prepare",
        json={
            **identity, "run_id": run_id, "phase": "main",
            "block_code": first_block, "block_order": 1,
            "trial_in_block": 1, "trial_sequence": 1,
        },
    )
    assert first_round.status_code == 409  # practice must be completed first

    practice_1 = client.post(
        "/api/experiment/ultimatum/prepare",
        json={
            **identity, "run_id": run_id, "phase": "practice",
            "block_code": "practice_proposer_agent", "block_order": 0,
            "trial_in_block": 1, "trial_sequence": 0,
        },
    )
    assert practice_1.status_code == 200, practice_1.text
    _resolve_ultimatum_round(client, identity, run_id, practice_1.json()["round_id"], offer=0)
    practice_2 = client.post(
        "/api/experiment/ultimatum/prepare",
        json={
            **identity, "run_id": run_id, "phase": "practice",
            "block_code": "practice_responder_coworker", "block_order": 0,
            "trial_in_block": 1, "trial_sequence": 0,
        },
    )
    assert practice_2.status_code == 200, practice_2.text
    _resolve_ultimatum_round(client, identity, run_id, practice_2.json()["round_id"], decision="accept")

    # Re-create the first main round now that practice is complete.
    first_round = client.post(
        "/api/experiment/ultimatum/prepare",
        json={
            **identity, "run_id": run_id, "phase": "main",
            "block_code": first_block, "block_order": 1,
            "trial_in_block": 1, "trial_sequence": 1,
        },
    )
    assert first_round.status_code == 200, first_round.text
    skipped = client.post(
        "/api/experiment/ultimatum/prepare",
        json={
            **identity, "run_id": run_id, "phase": "main",
            "block_code": first_block, "block_order": 1,
            "trial_in_block": 2, "trial_sequence": 2,
        },
    )
    assert skipped.status_code == 409  # cannot move on while round 1 is unresolved

    first_result = _resolve_ultimatum_round(
        client, identity, run_id, first_round.json()["round_id"], offer=0
    )
    assert first_result["participant_share"] == 10
    assert first_result["opponent_share"] == 0
    assert first_result["accepted"] is False
    assert first_result["points_earned"] == 0
    assert first_result["potential_points_lost"] == 10

    main_results = [first_result]
    for block_index, block_code in enumerate(block_order, start=1):
        first_trial = 2 if block_index == 1 else 1
        for trial in range(first_trial, main.ULTIMATUM_MAIN_BLOCK_TRIALS + 1):
            sequence = (block_index - 1) * main.ULTIMATUM_MAIN_BLOCK_TRIALS + trial
            prepared = client.post(
                "/api/experiment/ultimatum/prepare",
                json={
                    **identity, "run_id": run_id, "phase": "main",
                    "block_code": block_code, "block_order": block_index,
                    "trial_in_block": trial, "trial_sequence": sequence,
                },
            )
            assert prepared.status_code == 200, prepared.text
            round_data = prepared.json()
            if round_data["role"] == "proposer":
                assert round_data["participant_share"] is None
            else:
                assert 0 <= round_data["participant_share"] <= 10
                assert round_data["participant_share"] + round_data["opponent_share"] == 10
            if round_data["role"] == "proposer":
                # Offer 1..5 in subsequent proposer trials; offer 0 was tested above.
                response_data = _resolve_ultimatum_round(
                    client, identity, run_id, round_data["round_id"], offer=trial
                )
            else:
                decision = "accept" if trial % 2 else "reject"
                response_data = _resolve_ultimatum_round(
                    client, identity, run_id, round_data["round_id"], decision=decision
                )
            assert response_data["points_earned"] == (
                response_data["participant_share"] if response_data["accepted"] else 0
            )
            assert response_data["potential_points_lost"] == (
                0 if response_data["accepted"] else response_data["participant_share"]
            )
            main_results.append(response_data)

    assert len(main_results) == main.ULTIMATUM_MAIN_TRIALS
    assert {item["block_code"] for item in main_results} == set(main.ULTIMATUM_BLOCKS)
    final_score = sum(item["points_earned"] for item in main_results)
    final_lost = sum(item["potential_points_lost"] for item in main_results)
    block_summaries = {
        code: {
            "block_order": index,
            "points_earned": sum(item["points_earned"] for item in main_results if item["block_code"] == code),
            "potential_points_lost": sum(
                item["potential_points_lost"] for item in main_results if item["block_code"] == code
            ),
        }
        for index, code in enumerate(block_order, start=1)
    }
    completed = client.post(
        "/api/experiment/stages/complete",
        json={
            **identity,
            "stage_code": "ultimatum",
            "run_id": run_id,
            "final_payload": {
                "schema_version": 1,
                "condition_code": "A",
                "feedback_group": "Frame",
                "block_order": block_order,
                "final_score": final_score,
                "total_potential_points_lost": final_lost,
                "block_summaries": block_summaries,
                "practice_excluded_from_score": True,
            },
        },
    )
    assert completed.status_code == 200, completed.text

    with main.conn() as connection:
        stage = connection.execute(
            "SELECT status,current_run_id FROM experiment_stage_progress "
            "WHERE stage_code='ultimatum' AND current_run_id=?",
            (run_id,),
        ).fetchone()
        rows = connection.execute(
            "SELECT phase,status,opponent_actual_type,COUNT(*) AS n "
            "FROM ultimatum_trials WHERE run_id=? GROUP BY phase,status,opponent_actual_type",
            (run_id,),
        ).fetchall()
    assert stage["status"] == "completed"
    counts = {(row["phase"], row["status"], row["opponent_actual_type"]): row["n"] for row in rows}
    assert counts[("practice", "completed", "computer")] == 2
    assert counts[("main", "completed", "computer")] == main.ULTIMATUM_MAIN_TRIALS

    monkeypatch.setattr(main, "ADMIN_TOKEN", "ultimatum-export-token")
    exported = client.get(
        "/api/admin/ultimatum/export.csv",
        headers={"x-admin-token": "ultimatum-export-token"},
    )
    assert exported.status_code == 200, exported.text
    csv_rows = list(csv.DictReader(io.StringIO(exported.text.lstrip("\ufeff"))))
    assert len(csv_rows) == main.ULTIMATUM_MAIN_TRIALS + 2
    assert sum(row["phase"] == "practice" for row in csv_rows) == 2
    assert sum(row["phase"] == "main" for row in csv_rows) == main.ULTIMATUM_MAIN_TRIALS
    assert all(row["run_id"] == run_id for row in csv_rows)
    assert all(row["feedback_group"] == "Frame" for row in csv_rows)
    assert all(row["opponent_actual_type"] == "computer" for row in csv_rows)
    assert all(row["final_score"] == str(final_score) for row in csv_rows)
    headers = exported.text.lstrip("\ufeff").splitlines()[0].split(",")
    assert len(headers) == len(set(headers))
    assert {
        "normal_mean", "normal_sd", "normal_ai_self_share", "binomial_n",
        "binomial_p", "binomial_draw", "client_metadata_json",
        "client_metadata_resolve_json", "final_stage_payload_json", "payload_json",
    }.issubset(set(headers))
    assert any(row["normal_mean"] == "5.0" for row in csv_rows if row["role"] == "responder")
    assert any(row["binomial_n"] == "10" for row in csv_rows if row["role"] == "proposer")
    assert any("offer_model" in row["payload_json"] for row in csv_rows if row["role"] == "responder")
    assert any("decision_model" in row["payload_json"] for row in csv_rows if row["role"] == "proposer")
