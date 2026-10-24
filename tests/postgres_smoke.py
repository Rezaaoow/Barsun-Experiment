import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient

import main


def main_test():
    with TestClient(main.app) as client:
        created = client.post(
            "/api/session",
            json={
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
                "SELECT trial_count FROM sessions WHERE session_id=%s",
                (session_id,),
            ).fetchone()
            total = connection.execute(
                "SELECT COUNT(*) AS n FROM trials WHERE session_id=%s",
                (session_id,),
            ).fetchone()["n"]

        assert session["trial_count"] == 2, session
        assert total == 2, total

        exported = client.get("/api/admin/export.csv", headers={"x-admin-token": "ci-admin-token"})
        assert exported.status_code == 200, exported.text
        assert "server_session_id" in exported.text
        assert session_id in exported.text

        completed = client.post(
            "/api/complete",
            json={"session_id": session_id, "status": "completed", "trial_count": 2},
        )
        assert completed.status_code == 200, completed.text

    print("PostgreSQL smoke tests passed")


if __name__ == "__main__":
    main_test()
