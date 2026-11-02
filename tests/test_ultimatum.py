import csv
import io
import json
import os
import sys
import tempfile
import uuid
from pathlib import Path
from urllib.parse import parse_qs, urlparse

# Ensure this test module never imports the application with production credentials.
os.environ["DATABASE_URL"] = ""
os.environ.setdefault(
    "SQLITE_PATH",
    os.path.join(tempfile.gettempdir(), "ultimatum-test-" + uuid.uuid4().hex + ".sqlite3"),
)
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from fastapi.testclient import TestClient

import barsan_experiment as main


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DB_PATH", str(tmp_path / "ultimatum.sqlite3"))
    main.init_db()
    with TestClient(main.app) as test_client:
        yield test_client


def create_active_ultimatum(client, monkeypatch, participant_code="UT-TEST-01", condition="A"):
    monkeypatch.setattr(main, "ADMIN_TOKEN", "test-admin-token")
    assigned = client.post(
        "/api/admin/assignments",
        headers={"x-admin-token": "test-admin-token"},
        json={"assignments": [{"participant_code": participant_code, "condition_code": condition}]},
    )
    assert assigned.status_code == 200, assigned.text
    invitation_url = assigned.json()["assignments"][0]["invitation_url"]
    token = parse_qs(urlparse(invitation_url).query)["invite"][0]
    identity = {"invitation_token": token, "participant_code": participant_code}
    consent = client.post("/api/experiment/consent", json={**identity, "accepted": True})
    assert consent.status_code == 200, consent.text

    # Seed the two already-completed prerequisite stages. Their behaviour is covered
    # by the existing questionnaire and Life-Trade tests; this test isolates Ultimatum.
    with main.conn() as connection:
        assignment = main._lookup_assignment(connection, token, participant_code)
        participation_id = assignment["participation_id"]
        timestamp = main.now()
        for stage_code in ("questionnaire", "dd_lifetrade"):
            instrument_code, version, _instrument_name = main.STAGE_TO_INSTRUMENT[stage_code]
            proposed_version_id = str(uuid.uuid4())
            connection.execute(
                "INSERT OR IGNORE INTO instrument_versions("
                "instrument_version_id,instrument_code,version,settings,created_at) VALUES(?,?,?,?,?)",
                (
                    proposed_version_id,
                    instrument_code,
                    version,
                    json.dumps({"test": True}),
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
                    0,
                    json.dumps({"test": True}),
                ),
            )
            connection.execute(
                "INSERT INTO experiment_stage_progress("
                "participation_id,stage_code,status,attempt_number,current_run_id,draft_payload,"
                "started_at,completed_at,updated_at) VALUES(?,?,'completed',1,?,'{}',?,?,?) "
                "ON CONFLICT(participation_id,stage_code) DO UPDATE SET "
                "status='completed',attempt_number=1,current_run_id=excluded.current_run_id,"
                "draft_payload='{}',started_at=excluded.started_at,completed_at=excluded.completed_at,"
                "updated_at=excluded.updated_at",
                (participation_id, stage_code, run_id, timestamp, timestamp, timestamp),
            )

    started = client.post(
        "/api/experiment/stages/start",
        json={**identity, "stage_code": "ultimatum"},
    )
    assert started.status_code == 200, started.text
    assert started.json()["stage_code"] == "ultimatum"
    return identity, started.json()["run_id"]


def persisted_block_order(run_id):
    with main.conn() as connection:
        row = connection.execute(
            "SELECT metadata FROM instrument_runs WHERE run_id=?",
            (run_id,),
        ).fetchone()
    assert row is not None
    metadata = row["metadata"]
    if isinstance(metadata, str):
        metadata = json.loads(metadata)
    return metadata["block_order"]


def prepare_round(client, identity, run_id, *, phase, block_code, block_order, trial, sequence):
    response = client.post(
        "/api/experiment/ultimatum/prepare",
        json={
            **identity,
            "run_id": run_id,
            "phase": phase,
            "block_code": block_code,
            "block_order": block_order,
            "trial_in_block": trial,
            "trial_sequence": sequence,
            "client_metadata": {"test_client": True, "screen_width": 390},
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def resolve_proposer(client, identity, run_id, round_id, offer_to_opponent):
    response = client.post(
        "/api/experiment/ultimatum/resolve",
        json={
            **identity,
            "run_id": run_id,
            "round_id": round_id,
            "participant_offer_to_opponent": offer_to_opponent,
            "decision_screen_shown_at": main.now(),
            "responded_at": main.now(),
            "reaction_time_ms": 321.5,
            "wait_duration_ms": 2000,
            "client_metadata": {"test_response": True},
            "client_event_timestamps": {
                "proposer_screen_shown_at": main.now(),
                "offer_submitted_at": main.now(),
                "waiting_started_at": main.now(),
                "waiting_ended_at": main.now(),
            },
        },
    )
    assert response.status_code == 200, response.text
    return response.json()

def resolve_responder(client, identity, run_id, round_id, decision, wait_ms=3000):
    response = client.post(
        "/api/experiment/ultimatum/resolve",
        json={
            **identity,
            "run_id": run_id,
            "round_id": round_id,
            "participant_decision": decision,
            "decision_screen_shown_at": main.now(),
            "responded_at": main.now(),
            "reaction_time_ms": 456.25,
            "wait_duration_ms": wait_ms,
            "client_metadata": {"test_response": True},
            "client_event_timestamps": {
                "waiting_started_at": main.now(),
                "waiting_ended_at": main.now(),
                "responder_offer_shown_at": main.now(),
                "participant_choice_at": main.now(),
            },
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def record_round_events(client, identity, run_id, round_id, event_timestamps):
    response = client.post(
        "/api/experiment/ultimatum/events",
        json={
            **identity,
            "run_id": run_id,
            "round_id": round_id,
            "event_timestamps": event_timestamps,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()

def test_ultimatum_practice_main_blocks_scoring_completion_and_export(client, monkeypatch):
    identity, run_id = create_active_ultimatum(client, monkeypatch)

    # Practice proposer: the person's offered amount is split consistently.
    practice_offer = prepare_round(
        client, identity, run_id, phase="practice", block_code="practice_proposer_agent",
        block_order=0, trial=1, sequence=0,
    )
    assert practice_offer["role"] == "proposer"
    assert practice_offer["participant_share"] is None
    assert practice_offer["opponent_share"] is None
    practice_proposer_result = resolve_proposer(client, identity, run_id, practice_offer["round_id"], 4)
    assert practice_proposer_result["phase"] == "practice"
    assert practice_proposer_result["participant_share"] == 6
    assert practice_proposer_result["opponent_share"] == 4
    assert practice_proposer_result["points_earned"] in {0, 6}
    assert practice_proposer_result["potential_points_lost"] in {0, 6}
    assert practice_proposer_result["total_score"] == 0  # practice never contributes to score

    # Practice responder: the AI offer is sampled and retained in the audit payload.
    practice_response = prepare_round(
        client, identity, run_id, phase="practice", block_code="practice_responder_coworker",
        block_order=0, trial=1, sequence=0,
    )
    assert practice_response["role"] == "responder"
    assert practice_response["participant_share"] + practice_response["opponent_share"] == 10
    practice_responder_result = resolve_responder(
        client, identity, run_id, practice_response["round_id"], "accept", wait_ms=4000
    )
    assert practice_responder_result["accepted"] is True
    assert practice_responder_result["points_earned"] == practice_response["participant_share"]
    assert practice_responder_result["potential_points_lost"] == 0
    assert practice_responder_result["total_score"] == 0

    # An incomplete main task cannot be marked complete.
    incomplete = client.post(
        "/api/experiment/stages/complete",
        json={
            **identity,
            "stage_code": "ultimatum",
            "run_id": run_id,
            "final_payload": {
                "final_score": 0,
                "total_potential_points_lost": 0,
                "block_order": [],
            },
        },
    )
    assert incomplete.status_code == 422, incomplete.text

    # Every participant completes all four block types; only the order differs.
    block_order = persisted_block_order(run_id)
    final_results = []
    for order, block_code in enumerate(block_order, start=1):
        for trial in range(1, 6):
            sequence = (order - 1) * 5 + trial
            prepared = prepare_round(
                client, identity, run_id, phase="main", block_code=block_code,
                block_order=order, trial=trial, sequence=sequence,
            )
            if block_code.startswith("proposer"):
                result = resolve_proposer(
                    client, identity, run_id, prepared["round_id"], offer_to_opponent=(trial + order) % 11
                )
            else:
                decision = "accept" if trial % 2 else "reject"
                result = resolve_responder(
                    client, identity, run_id, prepared["round_id"], decision,
                    wait_ms=3000 + trial * 100,
                )
            assert result["phase"] == "main"
            assert result["block_code"] == block_code
            assert result["block_order"] == order
            assert result["trial_in_block"] == trial
            assert result["participant_share"] + result["opponent_share"] == 10
            assert result["points_earned"] >= 0
            assert result["potential_points_lost"] >= 0
            final_results.append(result)

    assert len(final_results) == 20
    assert {row["block_code"] for row in final_results} == set(main.ULTIMATUM_BLOCKS)

    # Post-result events are append-only on retries and remain visible in CSV export.
    event_round_id = final_results[-1]["round_id"]
    result_shown_at = main.now()
    advance_clicked_at = main.now()
    saved_events = record_round_events(
        client,
        identity,
        run_id,
        event_round_id,
        {"result_screen_shown_at": result_shown_at, "advance_clicked_at": advance_clicked_at},
    )
    record_round_events(
        client,
        identity,
        run_id,
        event_round_id,
        {"result_screen_shown_at": main.now(), "advance_clicked_at": main.now()},
    )
    assert saved_events["event_timestamps"]["result_screen_shown_at"] == result_shown_at

    invalid_event = client.post(
        "/api/experiment/ultimatum/events",
        json={
            **identity,
            "run_id": run_id,
            "round_id": event_round_id,
            "event_timestamps": {"unapproved_event_name": main.now()},
        },
    )
    assert invalid_event.status_code == 422

    with main.conn() as connection:
        score_rows = connection.execute(
            "SELECT COALESCE(SUM(points_earned),0) AS score,"
            "COALESCE(SUM(potential_points_lost),0) AS lost "
            "FROM ultimatum_trials WHERE run_id=? AND phase='main' AND status='completed'",
            (run_id,),
        ).fetchone()
        run_count = connection.execute(
            "SELECT trial_count FROM instrument_runs WHERE run_id=?",
            (run_id,),
        ).fetchone()["trial_count"]
        row_count = connection.execute(
            "SELECT COUNT(*) AS n FROM ultimatum_trials WHERE run_id=?",
            (run_id,),
        ).fetchone()["n"]
        sample = connection.execute(
            "SELECT payload,opponent_actual_type FROM ultimatum_trials "
            "WHERE run_id=? AND phase='main' AND block_code='responder_agent' AND trial_in_block=1",
            (run_id,),
        ).fetchone()
    assert run_count == 22
    assert row_count == 22
    assert sample["opponent_actual_type"] == "computer"
    payload = json.loads(sample["payload"]) if isinstance(sample["payload"], str) else dict(sample["payload"])
    assert payload["offer_model"]["model"] == "discrete_truncated_normal"
    assert payload["offer_model"]["mean"] == 5.0
    assert payload["offer_model"]["sd"] == 4.0

    final_payload = {
        "schema_version": 1,
        "condition_code": "A",
        "feedback_group": "Frame",
        "block_order": block_order,
        "final_score": int(score_rows["score"]),
        "total_potential_points_lost": int(score_rows["lost"]),
        "practice_excluded_from_score": True,
    }
    completed = client.post(
        "/api/experiment/stages/complete",
        json={**identity, "stage_code": "ultimatum", "run_id": run_id, "final_payload": final_payload},
    )
    assert completed.status_code == 200, completed.text
    assert completed.json()["completed"] is True

    with main.conn() as connection:
        stage = connection.execute(
            "SELECT status,draft_payload FROM experiment_stage_progress WHERE current_run_id=?",
            (run_id,),
        ).fetchone()
    assert stage["status"] == "completed"
    saved_stage = stage["draft_payload"] if isinstance(stage["draft_payload"], dict) else json.loads(stage["draft_payload"])
    assert saved_stage["final_score"] == int(score_rows["score"])

    monkeypatch.setattr(main, "ADMIN_TOKEN", "test-admin-token")
    exported = client.get("/api/admin/ultimatum/export.csv", headers={"x-admin-token": "test-admin-token"})
    assert exported.status_code == 200, exported.text
    csv_text = exported.text.lstrip("\ufeff")
    csv_headers = next(csv.reader(io.StringIO(csv_text)))
    assert len(csv_headers) == len(set(csv_headers)), csv_headers
    csv_rows = list(csv.DictReader(io.StringIO(csv_text)))
    assert len(csv_rows) == 22
    assert sum(row["phase"] == "main" for row in csv_rows) == 20
    assert {row["block_code"] for row in csv_rows if row["phase"] == "main"} == set(main.ULTIMATUM_BLOCKS)
    assert all(row["opponent_actual_type"] == "computer" for row in csv_rows)
    assert all(row["run_id"] == run_id for row in csv_rows)
    assert "client_event_timestamps_json" in csv_headers
    assert "client_event_server_received_at" in csv_headers
    assert "result_screen_shown_at" in csv_headers
    assert "advance_clicked_at" in csv_headers
    assert "opponent_offer_generated_at" in csv_headers
    assert "opponent_response_generated_at" in csv_headers
    event_row = next(row for row in csv_rows if row["round_id"] == event_round_id)
    saved_csv_events = json.loads(event_row["client_event_timestamps_json"])
    assert saved_csv_events["result_screen_shown_at"] == result_shown_at
    assert saved_csv_events["advance_clicked_at"] == advance_clicked_at
    assert event_row["result_screen_shown_at"] == result_shown_at
    assert event_row["advance_clicked_at"] == advance_clicked_at
    assert event_row["client_event_server_received_at"]
    assert any(
        row["phase"] == "main" and row["role"] == "responder" and row["opponent_offer_generated_at"]
        for row in csv_rows
    )
    assert any(
        row["phase"] == "main" and row["role"] == "proposer" and row["opponent_response_generated_at"]
        for row in csv_rows
    )


def test_ultimatum_rejects_wrong_block_order_and_old_attempt(client, monkeypatch):
    identity, run_id = create_active_ultimatum(client, monkeypatch, participant_code="UT-TEST-02")

    # Main trials are not allowed until both practice rounds have been completed.
    practice_offer = prepare_round(
        client, identity, run_id, phase="practice", block_code="practice_proposer_agent",
        block_order=0, trial=1, sequence=0,
    )
    resolve_proposer(client, identity, run_id, practice_offer["round_id"], 4)
    practice_response = prepare_round(
        client, identity, run_id, phase="practice", block_code="practice_responder_coworker",
        block_order=0, trial=1, sequence=0,
    )
    resolve_responder(client, identity, run_id, practice_response["round_id"], "accept")

    block_order = persisted_block_order(run_id)
    first_block_code = block_order[0]
    first = prepare_round(
        client, identity, run_id, phase="main", block_code=first_block_code,
        block_order=1, trial=1, sequence=1,
    )
    wrong_block_code = next(code for code in main.ULTIMATUM_BLOCKS if code != first_block_code)
    other_block_same_position = client.post(
        "/api/experiment/ultimatum/prepare",
        json={
            **identity,
            "run_id": run_id,
            "phase": "main",
            "block_code": wrong_block_code,
            "block_order": 1,
            "trial_in_block": 1,
            "trial_sequence": 1,
        },
    )
    assert other_block_same_position.status_code == 409

    # Starting the same incomplete module again supersedes the old run.
    restarted = client.post(
        "/api/experiment/stages/start",
        json={**identity, "stage_code": "ultimatum"},
    )
    assert restarted.status_code == 200, restarted.text
    assert restarted.json()["run_id"] != run_id
    stale_resolve = client.post(
        "/api/experiment/ultimatum/resolve",
        json={
            **identity,
            "run_id": run_id,
            "round_id": first["round_id"],
            "participant_offer_to_opponent": 5,
        },
    )
    assert stale_resolve.status_code == 409



def test_ultimatum_ai_offer_uses_sd_four_and_symmetric_discrete_weights(monkeypatch):
    captured = {}

    def capture_choices(population, weights, k):
        captured["population"] = list(population)
        captured["weights"] = list(weights)
        captured["k"] = k
        return [5]

    monkeypatch.setattr(main.random, "choices", capture_choices)
    participant_share, offer_model = main._sample_ai_offer_to_participant()

    assert participant_share == 5
    assert offer_model["model"] == "discrete_truncated_normal"
    assert offer_model["mean"] == 5.0
    assert offer_model["sd"] == 4.0
    assert captured["population"] == list(range(11))
    assert captured["k"] == 1
    assert captured["weights"] == pytest.approx(list(reversed(captured["weights"])))
    assert offer_model["weights"] == pytest.approx(captured["weights"])


def test_combined_ultimatum_round_lookup_preserves_consent_and_not_found_errors(
    client, monkeypatch
):
    identity, run_id = create_active_ultimatum(
        client, monkeypatch, participant_code="UT-COMBINED-LOOKUP"
    )
    prepared = prepare_round(
        client, identity, run_id, phase="practice",
        block_code="practice_proposer_agent", block_order=0, trial=1, sequence=0,
    )

    token_hash = main.hashlib.sha256(identity["invitation_token"].encode("utf-8")).hexdigest()
    with main.conn() as connection:
        assignment = connection.execute(
            "SELECT assignment_id FROM experiment_assignments WHERE invitation_token_hash=?",
            (token_hash,),
        ).fetchone()
        assert assignment is not None
        connection.execute(
            "DELETE FROM experiment_consents WHERE assignment_id=? AND consent_version=?",
            (assignment["assignment_id"], main.CONSENT_VERSION),
        )

    resolve_payload = {
        **identity,
        "run_id": run_id,
        "round_id": prepared["round_id"],
        "participant_offer_to_opponent": 5,
    }
    denied_resolve = client.post(
        "/api/experiment/ultimatum/resolve",
        json=resolve_payload,
    )
    assert denied_resolve.status_code == 403
    assert denied_resolve.json()["detail"] == "رضایت برای مشارکت ثبت نشده است."

    denied_events = client.post(
        "/api/experiment/ultimatum/events",
        json={
            **identity,
            "run_id": run_id,
            "round_id": prepared["round_id"],
            "event_timestamps": {"result_screen_shown_at": main.now()},
        },
    )
    assert denied_events.status_code == 403
    assert denied_events.json()["detail"] == "رضایت برای مشارکت ثبت نشده است."

    with main.conn() as connection:
        connection.execute(
            "INSERT INTO experiment_consents(assignment_id,consent_version,accepted_at) VALUES(?,?,?)",
            (assignment["assignment_id"], main.CONSENT_VERSION, main.now()),
        )

    missing_round_id = str(uuid.uuid4())
    missing_resolve = client.post(
        "/api/experiment/ultimatum/resolve",
        json={**resolve_payload, "round_id": missing_round_id},
    )
    assert missing_resolve.status_code == 404

    missing_events = client.post(
        "/api/experiment/ultimatum/events",
        json={
            **identity,
            "run_id": run_id,
            "round_id": missing_round_id,
            "event_timestamps": {"result_screen_shown_at": main.now()},
        },
    )
    assert missing_events.status_code == 404



def test_post_decision_timing_events_can_arrive_after_stage_completion(client, monkeypatch):
    identity, run_id = create_active_ultimatum(
        client, monkeypatch, participant_code="UT-LATE-EVENT-ACK"
    )
    round_data = prepare_round(
        client, identity, run_id, phase="practice",
        block_code="practice_proposer_agent", block_order=0, trial=1, sequence=0,
    )
    result = resolve_proposer(client, identity, run_id, round_data["round_id"], 4)
    assert result["round_id"] == round_data["round_id"]

    # Simulate the participant completing this stage before a delayed auxiliary
    # timing-event acknowledgement can be delivered.
    with main.conn() as connection:
        connection.execute(
            "UPDATE experiment_stage_progress SET status='completed' WHERE current_run_id=?",
            (run_id,),
        )
        connection.execute(
            "UPDATE instrument_runs SET status='completed' WHERE run_id=?",
            (run_id,),
        )

    saved = record_round_events(
        client, identity, run_id, round_data["round_id"],
        {"result_screen_shown_at": main.now(), "advance_clicked_at": main.now()},
    )
    assert saved["saved"] is True
    assert "result_screen_shown_at" in saved["event_timestamps"]
