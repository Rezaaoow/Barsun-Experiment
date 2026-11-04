from test_task_resilience import client
from cumulative_score_checks import check_cumulative_exports
import barsan_experiment as main


def test_historical_cumulative_exports_across_pages_and_runs(client, monkeypatch):
    assert check_cumulative_exports(client)["passed"]
    # Empty main scope still traverses foreign UUID pages; it must not compute
    # the scores of other studies just to discard them afterwards.
    def scoped_scores(connection, run_ids):
        assert not run_ids
        return {}
    monkeypatch.setattr(main, "STUDY_CODE", "synthetic-empty-cumulative-scope")
    monkeypatch.setattr(main, "_ultimatum_export_scores", scoped_scores)
    response = client.get("/api/admin/ultimatum/records.csv",
                          headers={"X-Admin-Token": main.ADMIN_TOKEN})
    assert response.status_code == 200
    assert len(response.text.splitlines()) == 1
