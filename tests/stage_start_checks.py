"""Shared SQLite/PostgreSQL regression for ambiguous concurrent starts."""
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from urllib.parse import parse_qs, urlsplit


def check_concurrent_starts(main, client, admin_token):
    marker = "%s" if main.DATABASE_URL else "?"
    headers = {"x-admin-token": admin_token}

    def assignment(module):
        step = "concurrent-" + uuid.uuid4().hex
        code = "CONCURRENT-" + uuid.uuid4().hex
        response = client.post("/api/admin/assignments", headers=headers, json={
            "assignments": [{"participant_code": code, "condition_code": "A"}],
            "steps": [{"step_id": step, "module_code": module}],
        })
        assert response.status_code == 200
        row = response.json()["assignments"][0]
        identity = {"invitation_token": parse_qs(urlsplit(row["invitation_url"]).query)["invite"][0],
                    "participant_code": code}
        assert client.post("/api/experiment/consent", json={"first_name":"Synthetic", "last_name":"Participant", **identity, "accepted": True}).status_code == 200
        return {**identity, "stage_code": main._canonical_workflow_stage(module), "step_id": step}

    def race(payloads):
        barrier = Barrier(len(payloads))
        def send(payload):
            barrier.wait(timeout=10)
            return client.post("/api/experiment/stages/start", json=payload)
        with ThreadPoolExecutor(max_workers=len(payloads)) as executor:
            return list(executor.map(send, payloads))

    # Widen the old read-before-insert race without synchronizing inside the
    # new lock (which would deadlock a correct serial implementation).
    original = main._workflow_progress_rows
    def delayed_read(*args, **kwargs):
        result = original(*args, **kwargs)
        time.sleep(0.02)
        return result
    main._workflow_progress_rows = delayed_read
    try:
        for module in ("questionnaire", "dd_lifetrade", "ultimatum_frame", "ultimatum_control"):
            payload = {**assignment(module), "start_request_id": str(uuid.uuid4())}
            responses = race([payload] * 6)
            assert all(response.status_code == 200 for response in responses)
            runs = {response.json()["run_id"] for response in responses}
            assert len(runs) == 1
            assert {response.json()["attempt_number"] for response in responses} == {1}
            with main.conn() as connection:
                count = connection.execute(
                    "SELECT COUNT(*) AS n FROM experiment_stage_start_requests WHERE request_id=" + marker,
                    (payload["start_request_id"],),
                ).fetchone()["n"]
            assert count == 1

            # Concurrent new page IDs are separate attempts and must serialize
            # their attempt numbers while preserving all superseded runs.
            refreshed = race([{**payload, "start_request_id": str(uuid.uuid4())} for _ in range(4)])
            assert all(response.status_code == 200 for response in refreshed)
            assert {response.json()["attempt_number"] for response in refreshed} == {2, 3, 4, 5}
            assert len({response.json()["run_id"] for response in refreshed}) == 4
            stale = client.post("/api/experiment/stages/start", json=payload)
            assert stale.status_code == 409
            ids = list(runs) + [response.json()["run_id"] for response in refreshed]
            with main.conn() as connection:
                rows = connection.execute(
                    "SELECT status FROM instrument_runs WHERE run_id IN (" + ",".join([marker] * 5) + ")",
                    tuple(ids),
                ).fetchall()
            assert sorted(row["status"] for row in rows) == ["in_progress"] + ["superseded"] * 4

        # A globally unique ID reused for a different identity must return 409,
        # even if both initial requests arrive together; never a database 500.
        request_id = str(uuid.uuid4())
        conflicting = race([{**assignment("questionnaire"), "start_request_id": request_id} for _ in range(2)])
        assert sorted(response.status_code for response in conflicting) == [200, 409]
    finally:
        main._workflow_progress_rows = original
