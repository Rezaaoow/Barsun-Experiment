import csv
import io
import json
import os
import sys
import tempfile
import uuid
import zipfile
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
    assert "پنل مدیریت آزمایش" in page.text
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


def test_module_only_test_links_start_at_selected_stage(client):
    for stage_code, completed_prerequisites in (
        ("questionnaire", []),
        ("dd_lifetrade", ["questionnaire"]),
        ("ultimatum", ["questionnaire", "dd_lifetrade"]),
    ):
        response = client.post(
            "/api/admin/module-test-links",
            headers={"x-admin-token": "test-admin-token"},
            json={"stage_code": stage_code, "condition_code": "B"},
        )
        assert response.status_code == 200, response.text
        link = response.json()
        assert link["participant_code"] == "TEST"
        assert link["condition_code"] == "B"
        assert link["stage_code"] == stage_code
        assert link["prerequisites_marked_synthetic"] == completed_prerequisites
        assert link["study_code"].startswith(main.TEST_STUDY_PREFIX + "module-")

        identity = {"invitation_token": invite_token(link), "participant_code": "TEST"}
        consent = client.post("/api/experiment/consent", json={**identity, "accepted": True})
        assert consent.status_code == 200, consent.text
        identified = client.post("/api/experiment/identify", json=identity)
        assert identified.status_code == 200, identified.text
        payload = identified.json()
        assert payload["condition_code"] == "B"
        assert payload["test_module_only"] == stage_code
        assert payload["current_stage"] == stage_code
        assert [row["stage_code"] for row in payload["stages"] if row["status"] == "completed"] == completed_prerequisites


def test_admin_all_export_zip_contains_three_modules_and_scope_only(client):
    main_assignment = create_main_assignment(client, code="MAIN-ZIP", condition="A")
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
    seed_completed_module_data(main_record["participation_id"], "MAINZIP")
    for index, record in enumerate(test_records):
        seed_completed_module_data(record["participation_id"], f"TESTZIP{index}")

    for scope, expected_code in (("main", "MAIN-ZIP"), ("test", "TEST")):
        response = client.get(
            f"/api/admin/export-all.zip?scope={scope}",
            headers={"x-admin-token": "test-admin-token"},
        )
        assert response.status_code == 200, response.text
        assert response.headers["content-type"].startswith("application/zip")
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            assert {"questionnaire.csv", "lifetrade.csv", "ultimatum.csv", "participant_progress.csv", "README.txt"} <= set(archive.namelist())
            for name in ("questionnaire.csv", "lifetrade.csv", "ultimatum.csv"):
                content = archive.read(name).decode("utf-8-sig")
                rows = list(csv.DictReader(io.StringIO(content)))
                assert rows
                assert {row["participant_code"] for row in rows} == {expected_code}


def test_scoped_reset_requires_exact_phrase_and_does_not_cross_scopes(client):
    main_assignment = create_main_assignment(client, code="MAIN-RESET", condition="B")
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
    seed_completed_module_data(main_record["participation_id"], "MAINRESET")
    for index, record in enumerate(test_records):
        seed_completed_module_data(record["participation_id"], f"TESTRESET{index}")

    preview = client.get(
        "/api/admin/maintenance/preview?scope=main",
        headers={"x-admin-token": "test-admin-token"},
    )
    assert preview.status_code == 200, preview.text
    assert preview.json()["counts"]["assignments"] == 1
    assert preview.json()["counts"]["lifetrade_trials"] == 1
    assert preview.json()["counts"]["ultimatum_trials"] == 1

    denied = client.post(
        "/api/admin/maintenance/reset",
        headers={"x-admin-token": "test-admin-token"},
        json={"scope": "main", "confirm_phrase": "CLEAR TEST DATA"},
    )
    assert denied.status_code == 422

    reset = client.post(
        "/api/admin/maintenance/reset",
        headers={"x-admin-token": "test-admin-token"},
        json={"scope": "main", "confirm_phrase": "CLEAR MAIN DATA"},
    )
    assert reset.status_code == 200, reset.text
    assert reset.json()["deleted_rows"]["experiment_assignments"] == 1
    assert reset.json()["participant_master_rows_retained"] is True

    main_rows = csv_rows(client, "/api/admin/export.csv")
    test_rows = csv_rows(client, "/api/admin/export.csv?scope=test")
    assert main_rows == []
    assert len(test_rows) == 2
    assert {row["participant_code"] for row in test_rows} == {"TEST"}

    # Scope=test cleanup removes only all test study rows and invalidates their links.
    test_reset = client.post(
        "/api/admin/maintenance/reset",
        headers={"x-admin-token": "test-admin-token"},
        json={"scope": "test", "confirm_phrase": "CLEAR TEST DATA"},
    )
    assert test_reset.status_code == 200, test_reset.text
    assert test_reset.json()["deleted_rows"]["test_studies"] >= 2
    assert csv_rows(client, "/api/admin/export.csv?scope=test") == []
