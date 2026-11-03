from test_task_resilience import client
from timing_checks import check_client_timing

def test_checkpoint_replay_concurrency_out_of_order_and_late_attempt(client):
    check_client_timing(client)
