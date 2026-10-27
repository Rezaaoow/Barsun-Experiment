import csv
import io
import json
import os
import sys
import tempfile
import uuid
from pathlib import Path
from urllib.parse import parse_qs, urlparse

os.environ["DATABASE_URL"] = ""
os.environ.setdefault(
    "SQLITE_PATH",
    os.path.join(tempfile.gettempdir(), "barsan-admin-links-test-" + uuid.uuid4().hex + ".sqlite3"),
)
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from fastapi.testclient import TestClient

import barsan_experiment as main


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DB_PATH", str(tmp_path / "admin-links.sqlite3"))
    monkeypatch.setattr(main, "ADMIN_TOKEN", "test-admin-token")
    main.init_db()
    with TestClient(main.app) as test_client:
        yield test_client


def invite_token(row):
    return parse_qs(urlparse(row["invitation_url"]).query)["invite"][0]


def create_main_assignment(client, code="MAIN-DATA", condition="B"):
    response = client.post(
        "/api/admin/assignments",
        headers={"x-admin-token": "test-admin-token"},
        json={"assignments": [{"participant_code": code, "condition_code": condition}]},
    )
    assert response.status_code == 200, response.text
    return response.json()["assignments"][0]


def seed_completed_module_data(participation_id, code_suffix):
    timestamp = main.now()
    with main.conn() as connection:
        for stage_code in ("questionnaire", "dd_lifetrade", "ultimatum"):
            instrument_code, version, instrument_name = main.STAGE_TO_INSTRUMENT[stage_code]
            connection.execute(
                "INSERT OR IGNORE INTO instruments(instrument_code,instrument_name,created_at) VALUES(?,?,?)",
                (instrument_code, instrument_name, timestamp),
            )
            connection.execute(
                "INSERT OR IGNORE INTO instrument_versions("
                "instrument_version_id,instrument_code,version,settings,created_at) VALUES(?,?,?,?,?)",
                (
                    str(uuid.uuid4()),
                    instrument_code,
                    version,
                    json.dumps({"test_seed": True}),
                    timestamp,
                ),
            )
            version_row = connection.execute(
                "SELECT instrument_version_id FROM instrument_versions "
                "WHERE instrument_code=? AND version=?",
                (instrument_code, version),
            ).fetchone()
            run_id = str(uuid.uuid4())
            connection.execute(
                "INSERT INTO instrument_runs("
                "run_id,participation_id,instrument_version_id,started_at,completed_at,status,trial_count,metadata) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (
                    run_id,
                    participation_id,
                    version_row["instrument_version_id"],
                    timestamp,
                    timestamp,
                    "completed",
                    1,
                    json.dumps({"attempt_number": 1}),
                ),
            )
            if stage_code == "questionnaire":
                connection.execute(
                    "INSERT INTO questionnaire_responses(run_id,question_code,response_value,updated_at) "
                    "VALUES(?,?,?,?)",
                    (run_id, "B1", "4", timestamp),
                )
            elif stage_code == "dd_lifetrade":
                connection.execute(
                    "INSERT INTO lifetrade_trials(client_event_id,run_id,received_at,task_code,payload) "
                    "VALUES(?,?,?,?,?)",
                    (
                        f"lt-event-{code_suffix}",
                        run_id,
                        timestamp,
                        "LT",
                        json.dumps({"test_scope_marker": code_suffix, "choice": "Later"}),
                    ),
                )
            else:
                connection.execute(
                    "INSERT INTO ultimatum_trials("
                    "round_id,run_id,phase,block_code,block_order,trial_in_block,trial_sequence,"
                    "role,opponent_condition,status,points_earned,potential_points_lost,"
                    "started_at,received_at,completed_at,payload) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        str(uuid.uuid4()),
                        run_id,
                        "main",
                        "proposer_agent",
                        0,
                        1,
                        1,
                        "proposer",
                        "agent",
                        "completed",
                        3,
                        0,
                        timestamp,
                        timestamp,
                        timestamp,
                        json.dumps({"test_scope_marker": code_suffix}),
                    ),
                )


def csv_rows(client, path):
    response = client.get(path, headers={"x-admin-token": "test-admin-token"})
    assert response.status_code == 200, response.text
    return list(csv.DictReader(io.StringIO(response.content.decode("utf-8-sig"))))


def test_test_links_use_code_test_and_create_separate_frame_control_studies(client):
    response = client.post("/api/admin/test-links", headers={"x-admin-token": "test-admin-token"})
    assert response.status_code == 200, response.text
    data = response.json()

    assert data["test_code"] == "TEST"
    assert data["created_count"] == 2
    assert [(row["participant_code"], row["group"], row["condition_code"]) for row in data["assignments"]] == [
        ("TEST", "Frame", "A"),
        ("TEST", "Control", "B"),
    ]
    assert len({row["study_code"] for row in data["assignments"]}) == 2
    assert all(row["study_code"].startswith(main.TEST_STUDY_PREFIX) for row in data["assignments"])

    for row in data["assignments"]:
        token = invite_token(row)
        identity = {"invitation_token": token, "participant_code": "TEST"}
        consent = client.post("/api/experiment/consent", json={**identity, "accepted": True})
        assert consent.status_code == 200, consent.text
        identified = client.post("/api/experiment/identify", json=identity)
        assert identified.status_code == 200, identified.text
        assert identified.json()["condition_code"] == row["condition_code"]

    page = client.get("/admin/links")
    assert page.status_code == 200
    assert "مدیریت لینک‌های اختصاصی" in page.text
    assert "ساخت دو لینک تست" in page.text


def test_main_and_test_exports_are_separated_across_all_modules(client):
    main_assignment = create_main_assignment(client)
    test_response = client.post("/api/admin/test-links", headers={"x-admin-token": "test-admin-token"})
    assert test_response.status_code == 200, test_response.text
    test_assignments = test_response.json()["assignments"]

    with main.conn() as connection:
        main_record = main._lookup_assignment(
            connection, invite_token(main_assignment), main_assignment["participant_code"]
        )
        test_records = [
            main._lookup_assignment(connection, invite_token(row), "TEST")
            for row in test_assignments
        ]
    seed_completed_module_data(main_record["participation_id"], "MAIN")
    for index, assignment in enumerate(test_records):
        seed_completed_module_data(assignment["participation_id"], f"TEST{index}")

    cases = (
        ("/api/admin/questionnaire/export.csv", "question_code"),
        ("/api/admin/export.csv", "test_scope_marker"),
        ("/api/admin/ultimatum/export.csv", "test_scope_marker"),
    )
    for route, marker_field in cases:
        main_rows = csv_rows(client, route)
        test_rows = csv_rows(client, route + "?scope=test")
        assert len(main_rows) == 1, (route, main_rows)
        assert len(test_rows) == 2, (route, test_rows)
        assert {row["participant_code"] for row in main_rows} == {"MAIN-DATA"}
        assert {row["participant_code"] for row in test_rows} == {"TEST"}
        assert all(row["study_code"].startswith(main.TEST_STUDY_PREFIX) for row in test_rows)
        assert all(not row["study_code"].startswith(main.TEST_STUDY_PREFIX) for row in main_rows)

    progress = client.get("/api/admin/progress", headers={"x-admin-token": "test-admin-token"})
    assert progress.status_code == 200, progress.text
    assert {row["participant_code"] for row in progress.json()} == {"MAIN-DATA"}


def test_test_link_generator_requires_admin_token(client):
    response = client.post("/api/admin/test-links")
    assert response.status_code == 401
