import json
import os
import tempfile
import time
import uuid
from pathlib import Path
from urllib.parse import parse_qs, urlparse

os.environ["DATABASE_URL"] = ""
os.environ.setdefault(
    "SQLITE_PATH",
    os.path.join(tempfile.gettempdir(), "barsan-workflow-test-" + uuid.uuid4().hex + ".sqlite3"),
)
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient
import pytest

import barsan_experiment as main


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DB_PATH", str(tmp_path / "workflow.sqlite3"))
    monkeypatch.setattr(main, "INVITATION_TOKEN_KEY_FILE", str(tmp_path / "private" / "invitation.key"))
    monkeypatch.setattr(main, "ADMIN_TOKEN", "test-admin-token")
    main.init_db()
    with TestClient(main.app) as test_client:
        yield test_client


def token_from_link(link):
    return parse_qs(urlparse(link["invitation_url"]).query)["invite"][0]


def create_workflow_assignment(client, steps, code="WORKFLOW-001"):
    response = client.post(
        "/api/admin/assignments",
        headers={"x-admin-token": "test-admin-token"},
        json={
            "assignments": [{"participant_code": code, "condition_code": "A"}],
            "steps": steps,
        },
    )
    assert response.status_code == 200, response.text
    link = response.json()["assignments"][0]
    identity = {"invitation_token": token_from_link(link), "participant_code": code}
    consent = client.post("/api/experiment/consent", json={**identity, "accepted": True})
    assert consent.status_code == 200, consent.text
    return link, identity


def complete_lifetrade_step(client, identity, step_id, run_id):
    response = client.post(
        "/api/experiment/stages/complete",
        json={
            **identity,
            "stage_code": "dd_lifetrade",
            "step_id": step_id,
            "run_id": run_id,
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["completed"] is True


def test_workflow_template_is_saved_and_repeated_modules_are_independent(client):
    steps = [
        {"step_id": "questionnaire-first", "module_code": "questionnaire"},
        {"step_id": "lifetrade-first", "module_code": "dd_lifetrade"},
        {"step_id": "lifetrade-second", "module_code": "dd_lifetrade"},
        {"step_id": "ultimatum-last", "module_code": "ultimatum"},
    ]
    saved = client.post(
        "/api/admin/workflow",
        headers={"x-admin-token": "test-admin-token"},
        json={"steps": steps},
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["steps"] == steps

    template = client.get(
        "/api/admin/workflow",
        headers={"x-admin-token": "test-admin-token"},
    )
    assert template.status_code == 200
    assert template.json()["steps"] == steps

    link, identity = create_workflow_assignment(client, steps)
    identified = client.post("/api/experiment/identify", json=identity)
    assert identified.status_code == 200, identified.text
    assert [step["step_id"] for step in identified.json()["workflow_steps"]] == [
        "questionnaire-first", "lifetrade-first", "lifetrade-second", "ultimatum-last"
    ]

    # Step 2 cannot start until step 1 is completed.
    blocked = client.post(
        "/api/experiment/stages/start",
        json={**identity, "stage_code": "dd_lifetrade", "step_id": "lifetrade-second"},
    )
    assert blocked.status_code == 409

    first = client.post(
        "/api/experiment/stages/start",
        json={**identity, "stage_code": "questionnaire", "step_id": "questionnaire-first"},
    )
    assert first.status_code == 200, first.text
    assert first.json()["step_id"] == "questionnaire-first"
    first_complete = client.post(
        "/api/experiment/stages/complete",
        json={
            **identity,
            "stage_code": "questionnaire",
            "step_id": "questionnaire-first",
            "run_id": first.json()["run_id"],
            "final_payload": {
                "answers": {code: "4" for code in main.QUESTIONNAIRE_QUESTION_CODES},
                "question_order": list(main.QUESTIONNAIRE_QUESTION_CODES),
                "schema_version": 1,
            },
        },
    )
    # Questionnaire completion requires persisted answers; use the answer endpoint for every item.
    if first_complete.status_code == 422:
        for index, question_code in enumerate(main.QUESTIONNAIRE_QUESTION_CODES):
            saved_answer = client.post(
                "/api/experiment/questionnaire/answer",
                json={
                    **identity,
                    "run_id": first.json()["run_id"],
                    "question_code": question_code,
                    "response_value": "4",
                    "current_question_index": index,
                },
            )
            assert saved_answer.status_code == 200, saved_answer.text
        first_complete = client.post(
            "/api/experiment/stages/complete",
            json={
                **identity,
                "stage_code": "questionnaire",
                "step_id": "questionnaire-first",
                "run_id": first.json()["run_id"],
                "final_payload": {
                    "answers": {code: "4" for code in main.QUESTIONNAIRE_QUESTION_CODES},
                    "question_order": list(main.QUESTIONNAIRE_QUESTION_CODES),
                    "schema_version": 1,
                },
            },
        )
    assert first_complete.status_code == 200, first_complete.text

    second = client.post(
        "/api/experiment/stages/start",
        json={**identity, "stage_code": "dd_lifetrade", "step_id": "lifetrade-first"},
    )
    assert second.status_code == 200, second.text
    complete_lifetrade_step(client, identity, "lifetrade-first", second.json()["run_id"])

    after_first = client.post("/api/experiment/identify", json=identity).json()
    assert after_first["current_step_id"] == "lifetrade-second"
    assert after_first["workflow_steps"][1]["status"] == "completed"
    assert after_first["workflow_steps"][2]["status"] == "not_started"

    third = client.post(
        "/api/experiment/stages/start",
        json={**identity, "stage_code": "dd_lifetrade", "step_id": "lifetrade-second"},
    )
    assert third.status_code == 200, third.text
    assert third.json()["run_id"] != second.json()["run_id"]
    complete_lifetrade_step(client, identity, "lifetrade-second", third.json()["run_id"])

    after_second = client.post("/api/experiment/identify", json=identity).json()
    assert after_second["current_step_id"] == "ultimatum-last"
    assert [step["status"] for step in after_second["workflow_steps"]] == [
        "completed", "completed", "completed", "not_started"
    ]


def test_full_test_link_is_one_snapshot_of_the_supplied_workflow(client):
    steps = [
        {"step_id": "test-dd", "module_code": "dd_lifetrade"},
        {"step_id": "test-ultimatum-frame", "module_code": "ultimatum_frame"},
        {"step_id": "test-dd-again", "module_code": "dd_lifetrade"},
    ]
    response = client.post(
        "/api/admin/test-links",
        headers={"x-admin-token": "test-admin-token"},
        json={"steps": steps},
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["created_count"] == 1
    assert data["workflow_modules"] == ["dd_lifetrade", "ultimatum_frame", "dd_lifetrade"]
    assert data["ultimatum_variant"] == "Frame"
    assert len(data["assignments"]) == 1
    link = data["assignments"][0]
    identity = {"invitation_token": token_from_link(link), "participant_code": "TEST"}
    consent = client.post("/api/experiment/consent", json={**identity, "accepted": True})
    assert consent.status_code == 200, consent.text
    identified = client.post("/api/experiment/identify", json=identity)
    assert identified.status_code == 200, identified.text
    payload = identified.json()
    assert [step["step_id"] for step in payload["workflow_steps"]] == [
        "test-dd", "test-ultimatum-frame", "test-dd-again"
    ]


def test_active_time_is_aggregated_by_module_for_monitoring_only(client):
    steps = [
        {"step_id": "dd-timed-1", "module_code": "dd_lifetrade"},
        {"step_id": "dd-timed-2", "module_code": "dd_lifetrade"},
    ]
    _, identity = create_workflow_assignment(client, steps, code="TIME-001")
    first = client.post(
        "/api/experiment/stages/start",
        json={**identity, "stage_code": "dd_lifetrade", "step_id": "dd-timed-1"},
    )
    assert first.status_code == 200, first.text
    started = client.post(
        "/api/experiment/time",
        json={**identity, "stage_code": "dd_lifetrade", "step_id": "dd-timed-1", "event": "start"},
    )
    assert started.status_code == 200, started.text
    time.sleep(0.08)
    paused = client.post(
        "/api/experiment/time",
        json={**identity, "stage_code": "dd_lifetrade", "step_id": "dd-timed-1", "event": "pause"},
    )
    assert paused.status_code == 200, paused.text
    complete_lifetrade_step(client, identity, "dd-timed-1", first.json()["run_id"])

    second = client.post(
        "/api/experiment/stages/start",
        json={**identity, "stage_code": "dd_lifetrade", "step_id": "dd-timed-2"},
    )
    assert second.status_code == 200, second.text
    started_again = client.post(
        "/api/experiment/time",
        json={**identity, "stage_code": "dd_lifetrade", "step_id": "dd-timed-2", "event": "start"},
    )
    assert started_again.status_code == 200, started_again.text
    time.sleep(0.08)
    completed = client.post(
        "/api/experiment/time",
        json={**identity, "stage_code": "dd_lifetrade", "step_id": "dd-timed-2", "event": "complete"},
    )
    assert completed.status_code == 200, completed.text

    monitor = client.get(
        "/api/admin/progress?scope=main&include_timing=true",
        headers={"x-admin-token": "test-admin-token"},
    )
    assert monitor.status_code == 200, monitor.text
    row = next(item for item in monitor.json() if item["participant_code"] == "TIME-001")
    assert row["total_active_seconds"] > 0
    assert row["module_time_seconds"]["dd_lifetrade"] > 0

    # Existing exports and default progress shape stay unchanged unless timing is requested.
    ordinary = client.get(
        "/api/admin/progress?scope=main",
        headers={"x-admin-token": "test-admin-token"},
    )
    assert ordinary.status_code == 200, ordinary.text
    ordinary_row = next(item for item in ordinary.json() if item["participant_code"] == "TIME-001")
    assert "module_time_seconds" not in ordinary_row
    assert "total_active_seconds" not in ordinary_row


def test_preexisting_default_links_are_compatible_with_active_time_tracking(client):
    response = client.post(
        "/api/admin/assignments",
        headers={"x-admin-token": "test-admin-token"},
        json={"assignments": [{"participant_code": "LEGACY-TIME-001", "condition_code": "B"}]},
    )
    assert response.status_code == 200, response.text
    link = response.json()["assignments"][0]
    identity = {"invitation_token": token_from_link(link), "participant_code": "LEGACY-TIME-001"}
    consent = client.post("/api/experiment/consent", json={**identity, "accepted": True})
    assert consent.status_code == 200, consent.text

    # Old assignments have no stored workflow snapshot, but the default step ids
    # still allow the existing three-stage participant flow to report its time.
    started = client.post(
        "/api/experiment/time",
        json={
            **identity,
            "stage_code": "questionnaire",
            "step_id": "default-questionnaire",
            "event": "start",
        },
    )
    assert started.status_code == 200, started.text
    time.sleep(0.05)
    paused = client.post(
        "/api/experiment/time",
        json={
            **identity,
            "stage_code": "questionnaire",
            "step_id": "default-questionnaire",
            "event": "pause",
        },
    )
    assert paused.status_code == 200, paused.text
    monitor = client.get(
        "/api/admin/progress?scope=main&include_timing=true",
        headers={"x-admin-token": "test-admin-token"},
    )
    assert monitor.status_code == 200, monitor.text
    row = next(item for item in monitor.json() if item["participant_code"] == "LEGACY-TIME-001")
    assert row["module_time_seconds"]["questionnaire"] > 0



def test_public_welcome_outline_matches_invite_workflow_and_hides_participant_data(client):
    steps = [
        {"step_id": "welcome-q", "module_code": "questionnaire"},
        {"step_id": "welcome-dd-one", "module_code": "dd_lifetrade"},
        {"step_id": "welcome-dd-two", "module_code": "dd_lifetrade"},
        {"step_id": "welcome-ult", "module_code": "ultimatum"},
    ]
    link, _identity = create_workflow_assignment(
        client, steps, code="WELCOME-PLAN-001"
    )
    invite = token_from_link(link)
    response = client.get(
        "/api/experiment/workflow",
        params={"invite": invite},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["total_steps"] == 4
    assert [step["module_code"] for step in payload["steps"]] == [
        "questionnaire", "dd_lifetrade", "dd_lifetrade", "ultimatum"
    ]
    assert payload["steps"][1]["repeat_number"] == 1
    assert payload["steps"][1]["repeat_total"] == 2
    assert payload["steps"][2]["repeat_number"] == 2
    assert payload["steps"][2]["repeat_total"] == 2
    assert payload["steps"][0]["title"] == "پرسشنامه"
    assert payload["steps"][3]["title"] == "بازی اولتیماتوم"
    assert "participant_code" not in payload
    assert "condition_code" not in payload
    assert "study_code" not in payload


def test_public_welcome_outline_rejects_invalid_or_inactive_invites(client):
    invalid = client.get(
        "/api/experiment/workflow",
        params={"invite": "not-a-valid-invitation-token"},
    )
    assert invalid.status_code == 404
    assert invalid.json()["detail"]

    link, _identity = create_workflow_assignment(
        client,
        [{"step_id": "welcome-only", "module_code": "questionnaire"}],
        code="WELCOME-INACTIVE-001",
    )
    invite = token_from_link(link)
    with main.conn() as connection:
        connection.execute(
            "UPDATE experiment_assignments SET active=0 WHERE invitation_token_hash=?",
            (main.hashlib.sha256(invite.encode("utf-8")).hexdigest(),),
        )
    inactive = client.get(
        "/api/experiment/workflow",
        params={"invite": invite},
    )
    assert inactive.status_code == 404


def test_workflow_variants_are_selected_by_timeline_and_reported_independently(client):
    frame_steps = [
        {"step_id": "frame-q", "module_code": "questionnaire"},
        {"step_id": "frame-dd-one", "module_code": "dd_lifetrade"},
        {"step_id": "frame-game", "module_code": "ultimatum_frame"},
        {"step_id": "frame-dd-two", "module_code": "dd_lifetrade"},
    ]
    control_steps = [
        {"step_id": "control-q", "module_code": "questionnaire"},
        {"step_id": "control-game", "module_code": "ultimatum_control"},
    ]
    frame_created = client.post(
        "/api/admin/assignments",
        headers={"x-admin-token": "test-admin-token"},
        json={"assignments": [{"participant_code": "VARIANT-FRAME"}], "steps": frame_steps},
    )
    control_created = client.post(
        "/api/admin/assignments",
        headers={"x-admin-token": "test-admin-token"},
        json={"assignments": [{"participant_code": "VARIANT-CONTROL"}], "steps": control_steps},
    )
    assert frame_created.status_code == 200, frame_created.text
    assert control_created.status_code == 200, control_created.text
    links = {
        "VARIANT-FRAME": frame_created.json()["assignments"][0],
        "VARIANT-CONTROL": control_created.json()["assignments"][0],
    }
    expected = {
        "VARIANT-FRAME": ["questionnaire", "dd_lifetrade", "ultimatum_frame", "dd_lifetrade"],
        "VARIANT-CONTROL": ["questionnaire", "ultimatum_control"],
    }

    with main.conn() as connection:
        assignments = {
            code: main._lookup_assignment(connection, token_from_link(link), code)
            for code, link in links.items()
        }
        timestamp = main.now()
        for code, assignment in assignments.items():
            stored = json.loads(assignment["workflow_json"])
            assert [step["module_code"] for step in stored] == expected[code]
            assert assignment["condition_code"] == ("A" if code == "VARIANT-FRAME" else "B")
            for index, step in enumerate(stored):
                complete = not (code == "VARIANT-CONTROL" and index == 1)
                connection.execute(
                    "INSERT INTO experiment_workflow_progress("
                    "assignment_id,participation_id,step_instance_id,step_order,stage_code,status,"
                    "attempt_number,started_at,completed_at,updated_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        assignment["assignment_id"], assignment["participation_id"], step["step_id"],
                        index + 1, main._canonical_workflow_stage(step["module_code"]),
                        "completed" if complete else "in_progress", 1, timestamp,
                        timestamp if complete else None, timestamp,
                    ),
                )

    for code, expected_modules in expected.items():
        public = client.get(
            "/api/experiment/workflow",
            params={"invite": token_from_link(links[code])},
        )
        assert public.status_code == 200, public.text
        assert [step["module_code"] for step in public.json()["steps"]] == expected_modules

    monitor = client.get(
        "/api/admin/progress?scope=main&include_timing=true",
        headers={"x-admin-token": "test-admin-token"},
    )
    assert monitor.status_code == 200, monitor.text
    rows = {row["participant_code"]: row for row in monitor.json()}
    frame = rows["VARIANT-FRAME"]
    control = rows["VARIANT-CONTROL"]
    assert (frame["completed_steps"], frame["total_steps"], frame["progress_percent"]) == (4, 4, 100)
    assert frame["progress_state"] == "completed"
    assert frame["ultimatum_variant"] == "Frame"
    assert [step["module_code"] for step in frame["workflow_steps"]] == expected["VARIANT-FRAME"]
    assert (control["completed_steps"], control["total_steps"], control["progress_percent"]) == (1, 2, 50)
    assert control["progress_state"] == "in_progress"
    assert control["ultimatum_variant"] == "Control"
    assert control["workflow_steps"][-1]["module_label"] == "Ultimatum — Control"

    preview = client.get(
        "/api/admin/maintenance/preview?scope=main",
        headers={"x-admin-token": "test-admin-token"},
    )
    assert preview.status_code == 200, preview.text
    assert preview.json()["counts"]["completed_experiments"] == 1


def test_workflow_config_accepts_a_single_ultimatum_variant(client):
    response = client.post(
        "/api/admin/workflow",
        headers={"x-admin-token": "test-admin-token"},
        json={"steps": [{"step_id": "frame-only", "module_code": "ultimatum_frame"}]},
    )
    assert response.status_code == 200, response.text
    assert [step["module_code"] for step in response.json()["steps"]] == ["ultimatum_frame"]



def test_previous_paired_builtin_default_is_migrated_to_neutral_workflow(client):
    previous_default = [
        {"step_id": "default-questionnaire", "module_code": "questionnaire"},
        {"step_id": "default-dd-lifetrade", "module_code": "dd_lifetrade"},
        {"step_id": "default-ultimatum-frame", "module_code": "ultimatum_frame"},
        {"step_id": "default-ultimatum-control", "module_code": "ultimatum_control"},
    ]
    with main.conn() as connection:
        connection.execute(
            "INSERT INTO study_workflows(study_code,workflow_json,updated_at) VALUES(?,?,?) "
            "ON CONFLICT(study_code) DO UPDATE SET workflow_json=excluded.workflow_json,updated_at=excluded.updated_at",
            (main.STUDY_CODE, json.dumps(previous_default), main.now()),
        )

    response = client.get(
        "/api/admin/workflow",
        headers={"x-admin-token": "test-admin-token"},
    )
    assert response.status_code == 200, response.text
    assert [step["module_code"] for step in response.json()["steps"]] == [
        "questionnaire", "dd_lifetrade"
    ]

def test_time_tracking_uses_atomic_updates_and_preserves_elapsed_cap(client):
    steps = [{"step_id": "atomic-time", "module_code": "questionnaire"}]
    _, identity = create_workflow_assignment(client, steps, code="ATOMIC-TIME-001")

    # Heartbeats cannot create a tracking row implicitly.
    before_start = client.post(
        "/api/experiment/time",
        json={**identity, "stage_code": "questionnaire", "step_id": "atomic-time", "event": "heartbeat"},
    )
    assert before_start.status_code == 409

    started = client.post(
        "/api/experiment/stages/start",
        json={**identity, "stage_code": "questionnaire", "step_id": "atomic-time"},
    )
    assert started.status_code == 200, started.text

    started_time = client.post(
        "/api/experiment/time",
        json={**identity, "stage_code": "questionnaire", "step_id": "atomic-time", "event": "start"},
    )
    assert started_time.status_code == 200, started_time.text
    assert started_time.json()["active_seconds"] == 0
    assert started_time.json()["is_active"] is True

    # A long gap is capped at 45 seconds, matching the previous Python implementation.
    with main.conn() as connection:
        connection.execute(
            "UPDATE experiment_time_tracking SET last_seen_at=datetime('now','-2 minutes') "
            "WHERE step_instance_id=?",
            ("atomic-time",),
        )
    heartbeat = client.post(
        "/api/experiment/time",
        json={**identity, "stage_code": "questionnaire", "step_id": "atomic-time", "event": "heartbeat"},
    )
    assert heartbeat.status_code == 200, heartbeat.text
    assert heartbeat.json()["active_seconds"] == 45
    assert heartbeat.json()["is_active"] is True

    paused = client.post(
        "/api/experiment/time",
        json={**identity, "stage_code": "questionnaire", "step_id": "atomic-time", "event": "pause"},
    )
    assert paused.status_code == 200, paused.text
    paused_seconds = paused.json()["active_seconds"]
    assert paused.json()["is_active"] is False

    # Even if last_seen_at is old, an inactive heartbeat must not add paused time.
    with main.conn() as connection:
        connection.execute(
            "UPDATE experiment_time_tracking SET last_seen_at=datetime('now','-2 hours') "
            "WHERE step_instance_id=?",
            ("atomic-time",),
        )
    inactive_heartbeat = client.post(
        "/api/experiment/time",
        json={**identity, "stage_code": "questionnaire", "step_id": "atomic-time", "event": "heartbeat"},
    )
    assert inactive_heartbeat.status_code == 200, inactive_heartbeat.text
    assert inactive_heartbeat.json()["active_seconds"] == paused_seconds
    assert inactive_heartbeat.json()["is_active"] is False

    resumed = client.post(
        "/api/experiment/time",
        json={**identity, "stage_code": "questionnaire", "step_id": "atomic-time", "event": "start"},
    )
    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["active_seconds"] == paused_seconds
    assert resumed.json()["is_active"] is True

    completed = client.post(
        "/api/experiment/time",
        json={**identity, "stage_code": "questionnaire", "step_id": "atomic-time", "event": "complete"},
    )
    assert completed.status_code == 200, completed.text
    assert completed.json()["is_active"] is False


def test_identify_checks_consent_as_part_of_invitation_lookup(client):
    response = client.post(
        "/api/admin/assignments",
        headers={"x-admin-token": "test-admin-token"},
        json={
            "assignments": [{"participant_code": "NO-CONSENT-001"}],
            "steps": [{"step_id": "consent-check", "module_code": "questionnaire"}],
        },
    )
    assert response.status_code == 200, response.text
    link = response.json()["assignments"][0]
    identity = {
        "invitation_token": token_from_link(link),
        "participant_code": "NO-CONSENT-001",
    }

    identified = client.post("/api/experiment/identify", json=identity)
    assert identified.status_code == 403
    assert identified.json()["detail"] == "رضایت برای مشارکت ثبت نشده است."

