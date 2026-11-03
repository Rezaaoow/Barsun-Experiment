"""Run the same task recovery contract against disposable SQLite and PostgreSQL."""
import csv
import io
import json
import uuid
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import parse_qs, urlparse

import barsan_experiment as main


def setup_module_run(client, stage, condition="A"):
    response = client.post("/api/admin/module-test-links", headers={"X-Admin-Token": main.ADMIN_TOKEN},
                           json={"stage_code": stage, "condition_code": condition})
    assert response.status_code == 200
    invitation = response.json()["invitation_url"]
    identity = {"invitation_token": parse_qs(urlparse(invitation).query)["invite"][0], "participant_code": "TEST"}
    assert client.post("/api/experiment/consent", json={**identity, "accepted": True}).status_code == 200
    step_id = "module-test-" + stage
    started = client.post("/api/experiment/stages/start", json={**identity, "stage_code": stage,
                          "step_id": step_id, "start_request_id": str(uuid.uuid4())})
    assert started.status_code == 200
    return identity, started.json(), step_id


def check_ultimatum_recovery(client, condition="A"):
    identity, started, step = setup_module_run(client, "ultimatum", condition)
    run_id = started["run_id"]
    order = started["draft_payload"]["block_order"]
    request = {"phase": "practice", "block_code": "practice_proposer_agent", "block_order": 0,
               "trial_in_block": 1, "trial_sequence": 0}
    prepared = client.post("/api/experiment/ultimatum/prepare", json={**identity, "run_id": run_id, **request})
    assert prepared.status_code == 200
    round_data = prepared.json()
    rounds, events = [], []
    for index in range(22):
        body = {**identity, "run_id": run_id, "round_id": round_data["round_id"], "prepare_next": True,
                "reaction_time_ms": 312.5, "decision_screen_shown_at": main.now(), "responded_at": main.now(),
                "client_metadata": {"test_device": True, "visibility_hidden": False},
                "client_event_timestamps": {"waiting_started_at": main.now()},
                "planned_wait_duration_ms": 2000, "wait_duration_ms": None}
        if round_data["role"] == "proposer":
            body["participant_offer_to_opponent"] = 4
        else:
            body["participant_decision"] = "accept"
        if events:
            body["auxiliary_events"] = events[-1:]
        if index == 0:
            with ThreadPoolExecutor(max_workers=6) as pool:
                responses = list(pool.map(lambda _: client.post("/api/experiment/ultimatum/resolve", json=body), range(6)))
            assert all(response.status_code == 200 for response in responses)
            assert len({response.json()["next_round"]["round_id"] for response in responses}) == 1
            result = responses[0].json()
        else:
            response = client.post("/api/experiment/ultimatum/resolve", json=body)
            assert response.status_code == 200
            result = response.json()
        if events:
            assert result["acknowledged_event_round_ids"] == [events[-1]["round_id"]]
        # Simulate a committed resolve whose HTTP acknowledgement was lost.
        replay = client.post("/api/experiment/ultimatum/resolve", json=body)
        assert replay.status_code == 200
        for field in ("round_id", "accepted", "points_earned", "total_score", "block_score", "total_potential_lost"):
            assert replay.json()[field] == result[field]
        assert replay.json().get("next_round") == result.get("next_round")
        rounds.append(result)
        events.append({"round_id": result["round_id"], "wait_duration_ms": 2017,
                       "event_timestamps": {"waiting_ended_at": main.now(), "result_screen_shown_at": main.now(),
                                            "advance_clicked_at": main.now()}})
        if index < 21:
            assert result.get("next_round") and result.get("next_round_request")
            round_data, request = result["next_round"], result["next_round_request"]
        else:
            assert result["next_round"] is None
    for offset in range(0, 22, 8):
        batch = {**identity, "run_id": run_id, "events": events[offset:offset + 8]}
        response = client.post("/api/experiment/ultimatum/events/batch", json=batch)
        assert response.status_code == 200
        assert client.post("/api/experiment/ultimatum/events/batch", json=batch).status_code == 200
    # A bad item rolls back the whole batch, including earlier valid updates.
    bad = client.post("/api/experiment/ultimatum/events/batch", json={**identity, "run_id": run_id, "events": [
        {"round_id": rounds[0]["round_id"], "event_timestamps": {"next_trial_clicked_at": main.now()}},
        {"round_id": "zz-missing-round", "event_timestamps": {"advance_clicked_at": main.now()}},
    ]})
    assert bad.status_code == 404
    marker = "%s" if main.DATABASE_URL else "?"
    with main.conn() as connection:
        saved = connection.execute("SELECT * FROM ultimatum_trials WHERE run_id=" + marker, (run_id,)).fetchall()
        run = connection.execute("SELECT trial_count FROM instrument_runs WHERE run_id=" + marker, (run_id,)).fetchone()
    assert len(saved) == 22 and run["trial_count"] == 22
    assert all(row["wait_duration_ms"] == 2017 for row in saved)
    original = next(row for row in saved if row["round_id"] == rounds[0]["round_id"])
    payload = main._json_value(original["payload"])
    assert "next_trial_clicked_at" not in payload["client_event_timestamps"]
    # A replay cannot replace first-write timing or regenerate a random result.
    late = {**identity, "run_id": run_id, "events": [{"round_id": rounds[0]["round_id"],
            "wait_duration_ms": 1, "event_timestamps": {"waiting_ended_at": main.now()}}]}
    assert client.post("/api/experiment/ultimatum/events/batch", json=late).status_code == 200
    final = {**identity, "run_id": run_id, "stage_code": "ultimatum", "step_id": step,
             "final_payload": {"block_order": order, "final_score": rounds[-1]["total_score"],
                               "total_potential_points_lost": rounds[-1]["total_potential_lost"]},
             "auxiliary_events": [{"round_id": rounds[-1]["round_id"], "event_timestamps": {"next_trial_clicked_at": main.now()}}]}
    assert client.post("/api/experiment/stages/complete", json=final).status_code == 200
    assert client.post("/api/experiment/stages/complete", json=final).json()["already_completed"] is True
    exported = client.get("/api/admin/ultimatum/records.csv?scope=test", headers={"X-Admin-Token": main.ADMIN_TOKEN})
    assert exported.status_code == 200
    rows = [row for row in csv.DictReader(io.StringIO(exported.text)) if row["run_id"] == run_id]
    assert len(rows) == 22
    row = next(row for row in rows if row["round_id"] == rounds[0]["round_id"])
    assert float(row["wait_duration_ms"]) == 2017
    assert json.loads(row["record_json"])["schema_version"] == 2
    assert json.loads(row["raw_payload_json"])["resolve_result"]["round_id"] == rounds[0]["round_id"]
    return run_id


def check_dd_recovery(client):
    identity, started, step = setup_module_run(client, "dd_lifetrade")
    run_id = started["run_id"]
    records = [{"client_event_id": str(uuid.uuid4()), "row": {"task": "LT" if i < 15 else "KT", "index": i,
               "trial_in_block": i % 5 + 1, "user_choice_type": "Now", "reaction_time_ms": 420.5,
               "left_image": "left", "left_stimulus": "left", "right_image": "right", "right_stimulus": "right",
               "participant_code": "untrusted-client-alias", "extra_research_field": {"kept": True}}} for i in range(30)]
    for batch in (records[:25], records[25:29]):
        response = client.post("/api/trials", json={"session_id": run_id, "trials": batch})
        assert response.status_code == 200
    completion = {**identity, "stage_code": "dd_lifetrade", "step_id": step, "run_id": run_id}
    assert client.post("/api/experiment/dd/complete", json=completion).status_code == 422
    assert client.post("/api/trials", json={"session_id": run_id, "trials": records[29:]}).status_code == 200
    replay = client.post("/api/trials", json={"session_id": run_id, "trials": records[:25]})
    assert replay.status_code == 200 and replay.json()["duplicate_count"] == 25
    assert client.post("/api/experiment/dd/complete", json=completion).status_code == 200
    assert client.post("/api/experiment/dd/complete", json=completion).json()["already_completed"] is True
    response = client.get("/api/admin/dd/records.csv?scope=test", headers={"X-Admin-Token": main.ADMIN_TOKEN})
    assert response.status_code == 200
    rows = [row for row in csv.DictReader(io.StringIO(response.text)) if row["run_id"] == run_id]
    assert len(rows) == 30 and all(row["participant_code"] == "TEST" for row in rows)
    assert all(json.loads(row["raw_payload_json"])["extra_research_field"] == {"kept": True} for row in rows)
    assert all(json.loads(row["record_json"])["stimulus"]["left"] == "left" for row in rows)
    return run_id
