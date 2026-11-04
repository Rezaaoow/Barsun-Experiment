from test_task_resilience import client
from cumulative_score_checks import check_cumulative_exports


def test_historical_cumulative_exports_across_pages_and_runs(client):
    assert check_cumulative_exports(client)["passed"]
