import io
import sys
import zipfile
from urllib.parse import parse_qs, urlparse
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient

import barsan_experiment as main
from stage_start_checks import check_concurrent_starts
from resilience_checks import check_dd_recovery, check_ultimatum_recovery


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

        # Exercise the single-query consent check and atomic PostgreSQL time tracking.
        full_test_link = test_links.json()["assignments"][0]
        full_test_identity = {
            "invitation_token": parse_qs(urlparse(full_test_link["invitation_url"]).query)["invite"][0],
            "participant_code": "TEST",
        }
        consent = client.post(
            "/api/experiment/consent",
            json={**full_test_identity, "accepted": True},
        )
        assert consent.status_code == 200, consent.text
        identified = client.post("/api/experiment/identify", json=full_test_identity)
        assert identified.status_code == 200, identified.text
        questionnaire_start = client.post(
            "/api/experiment/stages/start",
            json={
                **full_test_identity,
                "stage_code": "questionnaire",
                "step_id": "default-questionnaire",
            },
        )
        assert questionnaire_start.status_code == 200, questionnaire_start.text
        for event in ("start", "heartbeat", "pause"):
            tracked = client.post(
                "/api/experiment/time",
                json={
                    **full_test_identity,
                    "stage_code": "questionnaire",
                    "step_id": "default-questionnaire",
                    "event": event,
                },
            )
            assert tracked.status_code == 200, f"{event}: {tracked.text}"
        assert tracked.json()["is_active"] is False


        test_progress = client.get("/api/admin/progress?scope=test", headers=admin_headers)
        assert test_progress.status_code == 200, test_progress.text
        assert len(test_progress.json()) == 1, test_progress.json()
        assert all(row["study_code"].startswith("barsan-test-") for row in test_progress.json())

        preview = client.get("/api/admin/maintenance/preview?scope=test", headers=admin_headers)
        assert preview.status_code == 200, preview.text
        assert preview.json()["counts"]["assignments"] == 1

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

        # Link management on PostgreSQL: active CSV URLs are recoverable from encrypted tokens,
        # status changes preserve the URL, and deletion is limited to inactive assignments.
        links_response = client.get("/api/admin/assignments?scope=test&status=active", headers=admin_headers)
        assert links_response.status_code == 200, links_response.text
        links_payload = links_response.json()
        assert links_payload["counts"] == {"active": 1, "inactive": 0, "total": 1}
        assert len(links_payload["items"]) == 1
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
        assert keep_existing.json()["skipped_count"] == 1

        target_link = links_payload["items"][0]
        toggle_off = client.post(
            "/api/admin/assignments/status",
            headers=admin_headers,
            json={"scope": "test", "assignment_ids": [target_link["assignment_id"]], "active": False},
        )
        assert toggle_off.status_code == 200, toggle_off.text
        after_off = client.get("/api/admin/assignments?scope=test&status=all", headers=admin_headers)
        assert after_off.status_code == 200, after_off.text
        assert after_off.json()["counts"] == {"active": 0, "inactive": 1, "total": 1}

        toggle_on = client.post(
            "/api/admin/assignments/status",
            headers=admin_headers,
            json={"scope": "test", "assignment_ids": [target_link["assignment_id"]], "active": True},
        )
        assert toggle_on.status_code == 200, toggle_on.text
        after_on = client.get("/api/admin/assignments?scope=test&status=all", headers=admin_headers)
        assert after_on.status_code == 200, after_on.text
        assert after_on.json()["counts"] == {"active": 1, "inactive": 0, "total": 1}

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
        assert after_delete.json()["counts"] == {"active": 0, "inactive": 0, "total": 0}

        # Exercise Ultimatum prepare/resolve/idempotency and the grouped scoring
        # query against PostgreSQL without creating any live-study records.
        module_link_response = client.post(
            "/api/admin/module-test-links",
            headers=admin_headers,
            json={"stage_code": "ultimatum", "condition_code": "A"},
        )
        assert module_link_response.status_code == 200, module_link_response.text
        module_link = module_link_response.json()
        ultimatum_identity = {
            "invitation_token": parse_qs(urlparse(module_link["invitation_url"]).query)["invite"][0],
            "participant_code": "TEST",
        }
        ultimatum_consent = client.post(
            "/api/experiment/consent",
            json={**ultimatum_identity, "accepted": True},
        )
        assert ultimatum_consent.status_code == 200, ultimatum_consent.text
        ultimatum_start = client.post(
            "/api/experiment/stages/start",
            json={
                **ultimatum_identity,
                "stage_code": "ultimatum",
                "step_id": "module-test-ultimatum",
            },
        )
        assert ultimatum_start.status_code == 200, ultimatum_start.text
        ultimatum_run_id = ultimatum_start.json()["run_id"]

        def play_postgres_ultimatum_round(phase, block_code, block_order, trial, sequence, role):
            prepared = client.post(
                "/api/experiment/ultimatum/prepare",
                json={
                    **ultimatum_identity,
                    "run_id": ultimatum_run_id,
                    "phase": phase,
                    "block_code": block_code,
                    "block_order": block_order,
                    "trial_in_block": trial,
                    "trial_sequence": sequence,
                },
            )
            assert prepared.status_code == 200, prepared.text
            round_id = prepared.json()["round_id"]
            decision = {
                **ultimatum_identity,
                "run_id": ultimatum_run_id,
                "round_id": round_id,
            }
            if role == "proposer":
                decision["participant_offer_to_opponent"] = 5
            else:
                decision["participant_decision"] = "accept"
            resolved = client.post("/api/experiment/ultimatum/resolve", json=decision)
            assert resolved.status_code == 200, resolved.text
            # A repeated resolve must return the original result, not score twice.
            repeated = client.post("/api/experiment/ultimatum/resolve", json=decision)
            assert repeated.status_code == 200, repeated.text
            assert repeated.json() == resolved.json()
            return resolved.json()

        proposer_practice = play_postgres_ultimatum_round(
            "practice", "practice_proposer_agent", 0, 1, 0, "proposer"
        )
        responder_practice = play_postgres_ultimatum_round(
            "practice", "practice_responder_coworker", 0, 1, 0, "responder"
        )
        assert proposer_practice["phase"] == "practice"
        assert responder_practice["phase"] == "practice"

        first_block = ultimatum_start.json()["draft_payload"]["block_order"][0]
        first_role = main.ULTIMATUM_BLOCKS[first_block]["role"]
        first_main_result = play_postgres_ultimatum_round(
            "main", first_block, 1, 1, 1, first_role
        )
        assert first_main_result["phase"] == "main"
        assert first_main_result["total_score"] == first_main_result["block_score"]
        assert first_main_result["total_potential_lost"] == first_main_result["block_potential_lost"]

        event_save = client.post(
            "/api/experiment/ultimatum/events",
            json={
                **ultimatum_identity,
                "run_id": ultimatum_run_id,
                "round_id": first_main_result["round_id"],
                "event_timestamps": {
                    "result_screen_shown_at": main.now(),
                    "advance_clicked_at": main.now(),
                },
            },
        )
        assert event_save.status_code == 200, event_save.text

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

    with TestClient(main.app) as client:
        check_concurrent_starts(main, client, "ci-admin-token")
    print("PostgreSQL smoke, 24-participant and concurrent stage-start tests passed")

    with TestClient(main.app) as resilience_client:
        from export_checks import check_filtered_export_pages
        check_filtered_export_pages(resilience_client)
        from timing_checks import check_client_timing
        check_client_timing(resilience_client)
        previous_idle = main.EXPORT_IDLE_SECONDS
        try:
            main.EXPORT_IDLE_SECONDS = 90
            assert main._recent_task_activity()
            queued = resilience_client.get('/api/admin/dd/records.csv', headers={'X-Admin-Token': main.ADMIN_TOKEN})
            assert queued.status_code == 429 and queued.headers['X-Barsan-Export-Busy'] == '1'
            assert resilience_client.get('/api/health').status_code == 200
        finally:
            main.EXPORT_IDLE_SECONDS = previous_idle
        check_dd_recovery(resilience_client)
        check_ultimatum_recovery(resilience_client, "A")
        check_ultimatum_recovery(resilience_client, "B")
        from module_extension_checks import check_module_flow
        check_module_flow(resilience_client)
    print("PostgreSQL task recovery: DD + both Ultimatum variants passed")


if __name__ == "__main__":
    main_test()

