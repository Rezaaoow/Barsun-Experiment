from test_task_resilience import client
from export_checks import check_filtered_export_pages


def test_export_scope_is_preserved_after_empty_filtered_pages(client):
    check_filtered_export_pages(client)
