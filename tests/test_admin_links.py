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


def test_test_link_uses_one_exact_saved_workflow_and_not_a_frame_control_pair(client):
    steps = [
        {"step_id": "test-questionnaire", "module_code": "questionnaire"},
        {"step_id": "test-control", "module_code": "ultimatum_control"},
        {"step_id": "test-dd-again", "module_code": "dd_lifetrade"},
    ]
    saved = client.post(
        "/api/admin/workflow",
        headers={"x-admin-token": "test-admin-token"},
        json={"steps": steps},
    )
    assert saved.status_code == 200, saved.text

    response = client.post("/api/admin/test-links", headers={"x-admin-token": "test-admin-token"})
    assert response.status_code == 200, response.text
    data = response.json()

    assert data["test_code"] == "TEST"
    assert data["created_count"] == 1
    assert data["workflow_modules"] == ["questionnaire", "ultimatum_control", "dd_lifetrade"]
    assert data["ultimatum_variant"] == "Control"
    assert len(data["assignments"]) == 1
    link = data["assignments"][0]
    assert link["participant_code"] == "TEST"
    assert link["ultimatum_variant"] == "Control"
    assert link["study_code"].startswith(main.TEST_STUDY_PREFIX)

    token = invite_token(link)
    identity = {"invitation_token": token, "participant_code": "TEST"}
    consent = client.post("/api/experiment/consent", json={"first_name":"Synthetic", "last_name":"Participant", **identity, "accepted": True})
    assert consent.status_code == 200, consent.text
    identified = client.post("/api/experiment/identify", json=identity)
    assert identified.status_code == 200, identified.text
    payload = identified.json()
    assert [step["module_code"] for step in payload["workflow_steps"]] == data["workflow_modules"]

    page = client.get("/admin/links")
    assert page.status_code == 200
    assert "پنل مدیریت آزمایش" in page.text
    assert "ساخت لینک تست" in page.text
    assert "ساخت لینک تست (Frame و Control)" not in page.text

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
        assert len(test_rows) == 1, (route, test_rows)
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
        consent = client.post("/api/experiment/consent", json={"first_name":"Synthetic", "last_name":"Participant", **identity, "accepted": True})
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
    assert len(test_rows) == 1
    assert {row["participant_code"] for row in test_rows} == {"TEST"}

    # Scope=test cleanup removes only all test study rows and invalidates their links.
    test_reset = client.post(
        "/api/admin/maintenance/reset",
        headers={"x-admin-token": "test-admin-token"},
        json={"scope": "test", "confirm_phrase": "CLEAR TEST DATA"},
    )
    assert test_reset.status_code == 200, test_reset.text
    assert test_reset.json()["deleted_rows"]["test_studies"] >= 1
    assert csv_rows(client, "/api/admin/export.csv?scope=test") == []


def test_delete_inactive_assignments_removes_assignment_child_rows_safely(client):
    link = create_main_assignment(client, code="DELETE-CHILDREN", condition="B")
    token = invite_token(link)
    identity = {"invitation_token": token, "participant_code": "DELETE-CHILDREN"}
    consent = client.post("/api/experiment/consent", json={"first_name":"Synthetic", "last_name":"Participant", **identity, "accepted": True})
    assert consent.status_code == 200, consent.text

    with main.conn() as connection:
        assignment = main._lookup_assignment(
            connection, token, "DELETE-CHILDREN"
        )
        timestamp = main.now()
        connection.execute(
            "INSERT INTO experiment_workflow_progress("
            "assignment_id,participation_id,step_instance_id,step_order,stage_code,status,attempt_number,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (
                assignment["assignment_id"],
                assignment["participation_id"],
                "delete-step",
                1,
                "questionnaire",
                "in_progress",
                1,
                timestamp,
            ),
        )
        connection.execute(
            "INSERT INTO experiment_time_tracking("
            "assignment_id,step_instance_id,step_order,stage_code,active_seconds,is_active,started_at,last_seen_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (
                assignment["assignment_id"],
                "delete-step",
                1,
                "questionnaire",
                12.5,
                0,
                timestamp,
                timestamp,
            ),
        )
        connection.execute(
            "UPDATE experiment_assignments SET active=0 WHERE assignment_id=?",
            (assignment["assignment_id"],),
        )
        assignment_id = assignment["assignment_id"]

    deleted = client.post(
        "/api/admin/assignments/delete-inactive",
        headers={"x-admin-token": "test-admin-token"},
        json={"scope": "main", "confirm_phrase": "DELETE INACTIVE LINKS"},
    )
    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["deleted_count"] == 1

    with main.conn() as connection:
        assert connection.execute(
            "SELECT 1 FROM experiment_assignments WHERE assignment_id=?",
            (assignment_id,),
        ).fetchone() is None
        assert connection.execute(
            "SELECT 1 FROM experiment_consents WHERE assignment_id=?",
            (assignment_id,),
        ).fetchone() is None
        assert connection.execute(
            "SELECT 1 FROM experiment_workflow_progress WHERE assignment_id=?",
            (assignment_id,),
        ).fetchone() is None
        assert connection.execute(
            "SELECT 1 FROM experiment_time_tracking WHERE assignment_id=?",
            (assignment_id,),
        ).fetchone() is None


def test_active_links_csv_identifies_workflow_selected_ultimatum_variant(client):
    frame_steps = [
        {"step_id": "csv-q-frame", "module_code": "questionnaire"},
        {"step_id": "csv-frame", "module_code": "ultimatum_frame"},
    ]
    control_steps = [
        {"step_id": "csv-q-control", "module_code": "questionnaire"},
        {"step_id": "csv-control", "module_code": "ultimatum_control"},
    ]
    frame = client.post(
        "/api/admin/assignments",
        headers={"x-admin-token": "test-admin-token"},
        json={"assignments": [{"participant_code": "CSV-FRAME"}], "steps": frame_steps},
    )
    control = client.post(
        "/api/admin/assignments",
        headers={"x-admin-token": "test-admin-token"},
        json={"assignments": [{"participant_code": "CSV-CONTROL"}], "steps": control_steps},
    )
    assert frame.status_code == 200, frame.text
    assert control.status_code == 200, control.text

    response = client.get(
        "/api/admin/assignments/export.csv",
        headers={"x-admin-token": "test-admin-token"},
    )
    assert response.status_code == 200, response.text
    rows = list(csv.DictReader(io.StringIO(response.content.decode("utf-8-sig"))))
    variants = {row["participant_code"]: row["ultimatum_variant"] for row in rows}
    assert variants == {"CSV-FRAME": "Frame", "CSV-CONTROL": "Control"}


def create_group_link(client, label="Training group", scope="main"):
    response = client.post(
        "/api/admin/group-links",
        headers={"x-admin-token": "test-admin-token"},
        json={"scope": scope, "label": label},
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_group_link_claims_the_participants_own_code_and_creates_a_normal_invite(client):
    group = create_group_link(client)
    claim = client.post(
        "/api/experiment/group/claim",
        json={"first_name":"Synthetic", "last_name":"Participant",
            "group_token": parse_qs(urlparse(group["invitation_url"]).query)["group"][0],
            "participant_code": "BS-GROUP-101",
            "assign_random": False,
        },
    )
    assert claim.status_code == 200, claim.text
    payload = claim.json()
    assert payload["participant_code"] == "BS-GROUP-101"
    assert payload["invitation_token"]
    assert payload["reused"] is False

    consent = client.post(
        "/api/experiment/consent",
        json={"first_name":"Synthetic", "last_name":"Participant",
            "invitation_token": payload["invitation_token"],
            "participant_code": payload["participant_code"],
            "accepted": True,
        },
    )
    assert consent.status_code == 200, consent.text
    identity = client.post(
        "/api/experiment/identify",
        json={
            "invitation_token": payload["invitation_token"],
            "participant_code": payload["participant_code"],
        },
    )
    assert identity.status_code == 200, identity.text
    assert identity.json()["participant_code"] == "BS-GROUP-101"


def test_group_link_accepts_distinct_supplied_codes_without_individual_invites(client):
    group = create_group_link(client, label="Anonymous code group")
    group_token = parse_qs(urlparse(group["invitation_url"]).query)["group"][0]

    first = client.post(
        "/api/experiment/group/claim",
        json={"participant_code":"TEST-"+uuid.uuid4().hex, "first_name":"Synthetic", "last_name":"Participant", "group_token": group_token, "assign_random": False},
    )
    second = client.post(
        "/api/experiment/group/claim",
        json={"participant_code":"TEST-"+uuid.uuid4().hex, "first_name":"Synthetic", "last_name":"Participant", "group_token": group_token, "assign_random": False},
    )
    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    first_payload, second_payload = first.json(), second.json()
    assert first_payload["participant_code"].startswith("TEST-")
    assert second_payload["participant_code"].startswith("TEST-")
    assert first_payload["participant_code"] != second_payload["participant_code"]
    assert first_payload["invitation_token"] != second_payload["invitation_token"]


def test_group_link_claim_is_idempotent_for_the_same_participant_and_group(client):
    group = create_group_link(client)
    group_token = parse_qs(urlparse(group["invitation_url"]).query)["group"][0]
    request_body = {"first_name":"Synthetic", "last_name":"Participant",
        "group_token": group_token,
        "participant_code": "BS-GROUP-RETRY",
        "assign_random": False,
    }
    first = client.post("/api/experiment/group/claim", json=request_body)
    second = client.post("/api/experiment/group/claim", json=request_body)
    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert first.json()["invitation_token"] == second.json()["invitation_token"]
    assert first.json()["participant_code"] == second.json()["participant_code"]
    assert second.json()["reused"] is True

    with main.conn() as connection:
        rows = connection.execute(
            "SELECT COUNT(*) AS total FROM experiment_assignments WHERE group_link_id=?",
            (group["group_link_id"],),
        ).fetchone()
    assert rows["total"] == 1


def test_group_link_rejects_reuse_of_a_code_already_assigned_individually(client):
    create_main_assignment(client, code="BS-GROUP-EXISTING")
    group = create_group_link(client)
    group_token = parse_qs(urlparse(group["invitation_url"]).query)["group"][0]
    response = client.post(
        "/api/experiment/group/claim",
        json={"first_name":"Synthetic", "last_name":"Participant",
            "group_token": group_token,
            "participant_code": "BS-GROUP-EXISTING",
            "assign_random": False,
        },
    )
    assert response.status_code == 409


def test_group_link_can_be_listed_and_deactivated(client):
    group = create_group_link(client)
    headers = {"x-admin-token": "test-admin-token"}
    listed = client.get("/api/admin/group-links?scope=main", headers=headers)
    assert listed.status_code == 200, listed.text
    item = next(row for row in listed.json()["items"] if row["group_link_id"] == group["group_link_id"])
    assert item["active"] is True
    assert item["url_available"] is True

    disabled = client.post(
        "/api/admin/group-links/status",
        headers=headers,
        json={"scope": "main", "group_link_ids": [group["group_link_id"]], "active": False},
    )
    assert disabled.status_code == 200, disabled.text
    claim = client.post(
        "/api/experiment/group/claim",
        json={"first_name":"Synthetic", "last_name":"Participant",
            "group_token": parse_qs(urlparse(group["invitation_url"]).query)["group"][0],
            "participant_code": "BS-GROUP-DISABLED",
            "assign_random": False,
        },
    )
    assert claim.status_code == 404


def test_group_links_are_removed_by_scoped_data_reset(client):
    group = create_group_link(client, label="Reset group")
    group_token = parse_qs(urlparse(group["invitation_url"]).query)["group"][0]
    response = client.post(
        "/api/admin/maintenance/reset",
        headers={"x-admin-token": "test-admin-token"},
        json={"scope": "main", "confirm_phrase": "CLEAR MAIN DATA"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["deleted_rows"]["group_links"] == 1
    claim = client.post(
        "/api/experiment/group/claim",
        json={"first_name":"Synthetic", "last_name":"Participant",
            "group_token": group_token,
            "participant_code": "BS-GROUP-AFTER-RESET",
            "assign_random": False,
        },
    )
    assert claim.status_code == 404


def test_group_link_welcome_preview_and_admin_list_show_the_saved_stage_sequence(client):
    steps = [
        {"step_id": "welcome-q", "module_code": "questionnaire"},
        {"step_id": "welcome-dd", "module_code": "dd_lifetrade"},
        {"step_id": "welcome-ult", "module_code": "ultimatum_frame"},
    ]
    created = client.post(
        "/api/admin/group-links",
        headers={"x-admin-token": "test-admin-token"},
        json={"scope": "main", "label": "Welcome stages group", "steps": steps},
    )
    assert created.status_code == 200, created.text
    group = created.json()
    assert group["workflow_count"] == 3
    assert [step["module_code"] for step in group["workflow_steps"]] == [
        "questionnaire", "dd_lifetrade", "ultimatum_frame"
    ]
    token = parse_qs(urlparse(group["invitation_url"]).query)["group"][0]

    preview = client.get("/api/experiment/workflow?group=" + token)
    assert preview.status_code == 200, preview.text
    preview_steps = preview.json()["steps"]
    assert [step["module_code"] for step in preview_steps] == [
        "questionnaire", "dd_lifetrade", "ultimatum_frame"
    ]
    assert [step["title"] for step in preview_steps] == [
        "پرسشنامه", "تکلیف تصمیم‌گیری", "بازی اولتیماتوم — Frame"
    ]

    listed = client.get(
        "/api/admin/group-links?scope=main",
        headers={"x-admin-token": "test-admin-token"},
    )
    assert listed.status_code == 200, listed.text
    item = next(row for row in listed.json()["items"] if row["group_link_id"] == group["group_link_id"])
    assert item["workflow_count"] == 3
    assert [step["module_code"] for step in item["workflow_steps"]] == [
        "questionnaire", "dd_lifetrade", "ultimatum_frame"
    ]
    assert "پرسشنامه" in item["workflow_summary"]
    assert "Frame" in item["workflow_summary"]


def test_group_workflow_preview_rejects_inactive_or_unknown_group_token(client):
    response = client.get(
        "/api/experiment/workflow?group=" + "x" * 48,
    )
    assert response.status_code == 404

def test_named_group_claim_request_replay_returns_the_same_identity(client):
    group = create_group_link(client, label="Random claim replay group")
    group_token = parse_qs(urlparse(group["invitation_url"]).query)["group"][0]
    request_id = "b95f35ec-9d70-4f15-866d-3f0efbb48ea1"
    request_body = {"participant_code":"TEST-"+uuid.uuid4().hex, "first_name":"Synthetic", "last_name":"Participant",
        "group_token": group_token,
        "assign_random": False,
        "claim_request_id": request_id,
    }

    first = client.post("/api/experiment/group/claim", json=request_body)
    replay = client.post("/api/experiment/group/claim", json=request_body)
    assert first.status_code == 200, first.text
    assert replay.status_code == 200, replay.text
    first_payload, replay_payload = first.json(), replay.json()
    assert first_payload["participant_code"].startswith("TEST-")
    assert replay_payload["participant_code"] == first_payload["participant_code"]
    assert replay_payload["invitation_token"] == first_payload["invitation_token"]
    assert replay_payload["reused"] is True

    with main.conn() as connection:
        assignments = connection.execute(
            "SELECT COUNT(*) AS total FROM experiment_assignments WHERE group_link_id=?",
            (group["group_link_id"],),
        ).fetchone()
        claim_requests = connection.execute(
            "SELECT COUNT(*) AS total FROM experiment_group_claim_requests WHERE request_id=?",
            (request_id,),
        ).fetchone()
    assert int(assignments["total"]) == 1
    assert int(claim_requests["total"]) == 1

    # A request ID is bound to its original group and allocation mode.
    mismatched = client.post(
        "/api/experiment/group/claim",
        json={"first_name":"Synthetic", "last_name":"Participant",
            "group_token": group_token,
            "assign_random": False,
            "participant_code": "SOME-OTHER-CODE",
            "claim_request_id": request_id,
        },
    )
    assert mismatched.status_code == 409, mismatched.text


def test_group_claim_without_request_id_accepts_distinct_supplied_codes(client):
    group = create_group_link(client, label="Legacy random group")
    group_token = parse_qs(urlparse(group["invitation_url"]).query)["group"][0]
    first = client.post(
        "/api/experiment/group/claim",
        json={"participant_code":"TEST-"+uuid.uuid4().hex, "first_name":"Synthetic", "last_name":"Participant", "group_token": group_token, "assign_random": False},
    )
    second = client.post(
        "/api/experiment/group/claim",
        json={"participant_code":"TEST-"+uuid.uuid4().hex, "first_name":"Synthetic", "last_name":"Participant", "group_token": group_token, "assign_random": False},
    )
    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert first.json()["participant_code"] != second.json()["participant_code"]

