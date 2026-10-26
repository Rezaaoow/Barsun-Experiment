import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient

import barsan_experiment as main


def main_test():
    with TestClient(main.app) as client:
        created = client.post(
            "/api/session",
            json={
                "participant_code": "PG-SMOKE-0001",
                "experiment_version": "postgres-smoke",
                "user_agent": "ci",
                "screen_width": 390,
                "screen_height": 844,
            },
        )
        assert created.status_code == 200, created.text
        session_id = created.json()["session_id"]

        payload = {
            "session_id": session_id,
            "trials": [
                {"client_event_id": "pg-event-1", "row": {"index": 1, "choice": "Now"}},
                {"client_event_id": "pg-event-2", "row": {"index": 2, "choice": "Later"}},
            ],
        }
        first = client.post("/api/trials", json=payload)
        assert first.status_code == 200, first.text
        assert first.json()["inserted_count"] == 2, first.text

        retry = client.post("/api/trials", json=payload)
        assert retry.status_code == 200, retry.text
        assert retry.json()["inserted_count"] == 0, retry.text
        assert retry.json()["duplicate_count"] == 2, retry.text

        with main.conn() as connection:
            session = connection.execute(
                "SELECT trial_count FROM instrument_runs WHERE run_id=%s",
                (session_id,),
            ).fetchone()
            total = connection.execute(
                "SELECT COUNT(*) AS n FROM lifetrade_trials WHERE run_id=%s",
                (session_id,),
            ).fetchone()["n"]

        assert session["trial_count"] == 2, session
        assert total == 2, total

        exported = client.get("/api/admin/export.csv", headers={"x-admin-token": "ci-admin-token"})
        assert exported.status_code == 200, exported.text
        assert "server_session_id" in exported.text
        assert "participant_code" in exported.text
        assert "PG-SMOKE-0001" in exported.text
        assert session_id in exported.text

        completed = client.post(
            "/api/complete",
            json={"session_id": session_id, "status": "completed", "trial_count": 2},
        )
        assert completed.status_code == 200, completed.text

    # Exercise concurrent session creation and batched trial writes using the same
    # bounded PostgreSQL pool used by the deployed API.
    def concurrent_participant(index):
        with TestClient(main.app) as concurrent_client:
            created = concurrent_client.post(
                "/api/session",
                json={
                    "participant_code": f"PG-CONCURRENT-{index:04d}",
                    "experiment_version": "postgres-concurrency-smoke",
                },
            )
            assert created.status_code == 200, created.text
            concurrent_session_id = created.json()["session_id"]
            records = [
                {
                    "client_event_id": f"concurrent-{index}-event-{trial}",
                    "row": {"trial": trial, "participant_test_index": index},
                }
                for trial in range(5)
            ]
            saved = concurrent_client.post(
                "/api/trials",
                json={"session_id": concurrent_session_id, "trials": records},
            )
            assert saved.status_code == 200, saved.text
            assert saved.json()["inserted_count"] == 5, saved.text
            return concurrent_session_id

    with ThreadPoolExecutor(max_workers=24) as executor:
        concurrent_sessions = list(executor.map(concurrent_participant, range(24)))

    placeholders = ",".join(["%s"] * len(concurrent_sessions))
    with main.conn() as connection:
        total = connection.execute(
            f"SELECT SUM(trial_count) AS total FROM instrument_runs WHERE run_id IN ({placeholders})",
            tuple(concurrent_sessions),
        ).fetchone()["total"]
    assert total == 120, total

    print("PostgreSQL smoke and 24-participant concurrency tests passed")


if __name__ == "__main__":
    main_test()
