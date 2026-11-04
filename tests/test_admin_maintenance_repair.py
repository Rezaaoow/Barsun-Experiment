from test_admin_links import client
from admin_maintenance_checks import check_admin_maintenance


def test_admin_cleanup_status_and_group_deletion_with_full_receipts(client):
    check_admin_maintenance(client)
