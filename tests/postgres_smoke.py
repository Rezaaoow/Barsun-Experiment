import io
import sys
import zipfile
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

        # Exercise the isolated test scope on PostgreSQL. Passing an empty parameter
        # tuple alongside a literal LIKE pattern containing '%' previously raised a
        # psycopg formatting error, so all scope-specific admin queries are checked here.
        admin_headers = {"x-admin-token": "ci-admin-token"}
        test_links = client.post("/api/admin/test-links", headers=admin_headers)
        assert test_links.status_code == 200, test_links.text

        test_progress = client.get("/api/admin/progress?scope=test", headers=admin_headers)
        assert test_progress.status_code == 200, test_progress.text
        assert len(test_progress.json()) == 2, test_progress.json()
        assert all(row["study_code"].startswith("barsan-test-") for row in test_progress.json())

        preview = client.get("/api/admin/maintenance/preview?scope=test", headers=admin_headers)
        assert preview.status_code == 200, preview.text
        assert preview.json()["counts"]["assignments"] == 2

        for path in (
            "/api/admin/questionnaire/export.csv?scope=test",
            "/api/admin/export.csv?scope=test",
            "/api/admin/ultimatum/export.csv?scope=test",
        ):
            scoped_export = client.get(path, headers=admin_headers)
            assert scoped_export.status_code == 200, f"{path}: {scoped_export.text}"
            assert scoped_export.headers.get("content-type", "").startswith("text/csv")

        full_export = client.get("/api/admin/export-all.zip?scope=test", headers=admin_headers)
        assert full_export.status_code == 200, full_export.text
        with zipfile.ZipFile(io.BytesIO(full_export.content)) as archive:
            assert {"questionnaire.csv", "lifetrade.csv", "ultimatum.csv", "participant_progress.csv", "README.txt"} <= set(archive.namelist())

        # Configurable workflow, repeated module instances and active-time aggregation on PostgreSQL.
        configured_workflow = ["dd_lifetrade", "dd_lifetrade", "questionnaire"]
        workflow_saved = client.put(
            "/api/admin/workflow",
            headers=admin_headers,
            json={"workflow": configured_workflow},
        )
        assert workflow_saved.status_code == 200, workflow_saved.text

        assignment_response = client.post(
            "/api/admin/assignments",
            headers=admin_headers,
            json={"assignments": [{"participant_code": "PG-WORKFLOW-01", "condition_code": "A"}]},
        )
        assert assignment_response.status_code == 200, assignment_response.text
        workflow_url = assignment_response.json()["assignments"][0]["invitation_url"]
        workflow_token = workflow_url.split("invite=", 1)[1].split("&", 1)[0]
        workflow_identity = {
            "invitation_token": workflow_token,
            "participant_code": "PG-WORKFLOW-01",
        }

        opened = client.post(
            "/api/experiment/activity/open",
            json={"invitation_token": workflow_token},
        )
        assert opened.status_code == 200, opened.text
        consent = client.post(
            "/api/experiment/consent",
            json={**workflow_identity, "accepted": True},
        )
        assert consent.status_code == 200, consent.text
        identified = client.post("/api/experiment/identify", json=workflow_identity)
        assert identified.status_code == 200, identified.text
        assert identified.json()["workflow"] == configured_workflow

        first_step = client.post(
            "/api/experiment/stages/start",
            json={**workflow_identity, "stage_code": "dd_lifetrade", "workflow_step_index": 0},
        )
        assert first_step.status_code == 200, first_step.text
        first_run = first_step.json()["run_id"]
        blocked_second = client.post(
            "/api/experiment/stages/start",
            json={**workflow_identity, "stage_code": "dd_lifetrade", "workflow_step_index": 1},
        )
        assert blocked_second.status_code == 409

        first_ping = client.post(
            "/api/experiment/activity/ping",
            json={**workflow_identity, "ping_id": "pg-workflow-active-ping-0001",
                  "run_id": first_run, "active_seconds": 20},
        )
        assert first_ping.status_code == 200, first_ping.text
        first_done = client.post(
            "/api/experiment/stages/complete",
            json={**workflow_identity, "stage_code": "dd_lifetrade",
                  "workflow_step_index": 0, "run_id": first_run},
        )
        assert first_done.status_code == 200, first_done.text

        second_step = client.post(
            "/api/experiment/stages/start",
            json={**workflow_identity, "stage_code": "dd_lifetrade", "workflow_step_index": 1},
        )
        assert second_step.status_code == 200, second_step.text
        second_run = second_step.json()["run_id"]
        second_ping = client.post(
            "/api/experiment/activity/ping",
            json={**workflow_identity, "ping_id": "pg-workflow-active-ping-0002",
                  "run_id": second_run, "active_seconds": 35},
        )
        assert second_ping.status_code == 200, second_ping.text
        second_done = client.post(
            "/api/experiment/stages/complete",
            json={**workflow_identity, "stage_code": "dd_lifetrade",
                  "workflow_step_index": 1, "run_id": second_run},
        )
        assert second_done.status_code == 200, second_done.text

        progress_response = client.get("/api/admin/progress?scope=main", headers=admin_headers)
        assert progress_response.status_code == 200, progress_response.text
        workflow_row = next(row for row in progress_response.json()
                            if row["participant_code"] == "PG-WORKFLOW-01")
        assert workflow_row["first_opened_at"]
        assert workflow_row["total_active_seconds"] == 55.0
        assert [step["active_seconds"] for step in workflow_row["workflow_steps"]] == [20.0, 35.0, 0.0]
        assert workflow_row["current_step_index"] == 2
        assert workflow_row["current_stage"] == "questionnaire"

        # Link management on PostgreSQL: active CSV URLs are recoverable from encrypted tokens,
        # status changes preserve the URL, and deletion is limited to inactive assignments.
        links_response = client.get("/api/admin/assignments?scope=test&status=active", headers=admin_headers)
        assert links_response.status_code == 200, links_response.text
        links_payload = links_response.json()
        assert links_payload["counts"] == {"active": 2, "inactive": 0, "total": 2}
        assert len(links_payload["items"]) == 2
        assert all(item["url_available"] and "?invite=" in item["invitation_url"] for item in links_payload["items"])

        active_links_csv = client.get("/api/admin/assignments/export.csv?scope=test", headers=admin_headers)
        assert active_links_csv.status_code == 200, active_links_csv.text
        assert "invitation_url" in active_links_csv.text
        assert "url_available" in active_links_csv.text
        assert "barsan-test-" in active_links_csv.text

        keep_existing = client.post(
            "/api/admin/assignments/reissue-missing-links",
            headers=admin_headers,
            json={
                "scope": "test",
                "assignment_ids": [item["assignment_id"] for item in links_payload["items"]],
            },
        )
        assert keep_existing.status_code == 200, keep_existing.text
        assert keep_existing.json()["reissued_count"] == 0
        assert keep_existing.json()["skipped_count"] == 2

        target_link = links_payload["items"][0]
        toggle_off = client.post(
            "/api/admin/assignments/status",
            headers=admin_headers,
            json={"scope": "test", "assignment_ids": [target_link["assignment_id"]], "active": False},
        )
        assert toggle_off.status_code == 200, toggle_off.text
        after_off = client.get("/api/admin/assignments?scope=test&status=all", headers=admin_headers)
        assert after_off.status_code == 200, after_off.text
        assert after_off.json()["counts"] == {"active": 1, "inactive": 1, "total": 2}

        toggle_on = client.post(
            "/api/admin/assignments/status",
            headers=admin_headers,
            json={"scope": "test", "assignment_ids": [target_link["assignment_id"]], "active": True},
        )
        assert toggle_on.status_code == 200, toggle_on.text
        after_on = client.get("/api/admin/assignments?scope=test&status=all", headers=admin_headers)
        assert after_on.status_code == 200, after_on.text
        assert after_on.json()["counts"] == {"active": 2, "inactive": 0, "total": 2}

        # Finally prove that the confirmed bulk deletion only removes inactive links.
        client.post(
            "/api/admin/assignments/status",
            headers=admin_headers,
            json={"scope": "test", "assignment_ids": [target_link["assignment_id"]], "active": False},
        )
        delete_inactive = client.post(
            "/api/admin/assignments/delete-inactive",
            headers=admin_headers,
            json={"scope": "test", "confirm_phrase": "DELETE INACTIVE LINKS"},
        )
        assert delete_inactive.status_code == 200, delete_inactive.text
        assert delete_inactive.json()["deleted_count"] == 1
        after_delete = client.get("/api/admin/assignments?scope=test&status=all", headers=admin_headers)
        assert after_delete.status_code == 200, after_delete.text
        assert after_delete.json()["counts"] == {"active": 1, "inactive": 0, "total": 1}

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
