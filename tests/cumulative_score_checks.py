"""Shared SQLite/PostgreSQL historical export contract; synthetic data only."""
import csv
import io
import json
import uuid

import barsan_experiment as main
from resilience_checks import setup_module_run


def check_cumulative_exports(client):
    marker = "%s" if main.DATABASE_URL else "?"
    expected = {}
    originals = {}
    # 220 rows cross the records export's 200-row cursor boundary. UUIDs and
    # insertion order intentionally disagree with protocol order.
    for attempt in range(10):
        _, started, _ = setup_module_run(client, "ultimatum")
        run_id = started["run_id"]
        running = 0
        records = []
        for index in range(22):
            practice = index < 2
            phase = "practice" if practice else "main"
            points = 9 if practice else (index % 4)
            status = "prepared" if index == 21 else "completed"
            if not practice and status == "completed":
                running += points
            round_id = uuid.uuid4().hex
            payload = {"historical": True, "resolve_result": {"total_score": -999}}
            originals[round_id] = payload
            expected[round_id] = {"cumulative_score": running, "run_id": run_id}
            block = 0 if practice else 1 + (index - 2) // 5
            trial = index + 1 if practice else 1 + (index - 2) % 5
            records.append((round_id, run_id, phase, "block-" + str(block), block, trial,
                            index, "responder", "agent", status, points, main.now(),
                            main.now(), main._json_param(payload)))
        for item in expected.values():
            if item["run_id"] == run_id:
                item["run_total_score"] = running
        with main.conn() as connection:
            # Historical/superseded runs and missing completion payloads still
            # have reconstructable scores; never borrow another stage's score.
            connection.execute("UPDATE instrument_runs SET status=" + marker + " WHERE run_id=" + marker,
                               ("superseded" if attempt % 2 else "in_progress", run_id))
            for record in reversed(records):
                connection.execute(
                    "INSERT INTO ultimatum_trials(round_id,run_id,phase,block_code,block_order,trial_in_block,"
                    "trial_sequence,role,opponent_condition,status,points_earned,started_at,received_at,payload) "
                    "VALUES(" + ",".join([marker] * 14) + ")", record)
    for endpoint in ("export.csv", "records.csv"):
        response = client.get("/api/admin/ultimatum/" + endpoint + "?scope=test",
                              headers={"X-Admin-Token": main.ADMIN_TOKEN})
        assert response.status_code == 200
        rows = [row for row in csv.DictReader(io.StringIO(response.text.lstrip("\ufeff")))
                if row["round_id"] in expected]
        assert len(rows) == 220 and len({row["round_id"] for row in rows}) == 220
        for row in rows:
            score = expected[row["round_id"]]
            assert int(row["final_score"]) == int(row["cumulative_score"]) == score["cumulative_score"]
            assert int(row["run_total_score"]) == score["run_total_score"]
            raw_key = "raw_payload_json" if endpoint == "records.csv" else "payload_json"
            assert json.loads(row[raw_key]) == originals[row["round_id"]]
            if endpoint == "records.csv":
                assert json.loads(row["record_json"])["outcome"]["final_score"] == score["cumulative_score"]
    with main.conn() as connection:
        for round_id, payload in originals.items():
            stored = connection.execute("SELECT payload FROM ultimatum_trials WHERE round_id=" + marker,
                                        (round_id,)).fetchone()
            assert main._json_value(stored["payload"]) == payload
    return {"passed": True, "synthetic_rounds": 220, "runs": 10,
            "exports": 2, "pagination_checked": True, "historical_payloads_unchanged": True}
