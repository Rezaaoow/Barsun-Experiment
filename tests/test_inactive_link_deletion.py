from test_admin_links import client
from inactive_link_checks import check_inactive_link_deletion


def test_delete_inactive_link_with_group_start_and_time_receipts_preserves_research(client):
    check_inactive_link_deletion(client)


def test_delete_inactive_links_crosses_bounded_batch_and_replays_safely(client):
    import barsan_experiment as main
    headers={'X-Admin-Token':main.ADMIN_TOKEN}
    created=client.post('/api/admin/assignments',headers=headers,json={'assignments':[
        {'participant_code':f'BATCH-DELETE-{n}'} for n in range(205)]})
    assert created.status_code==200
    listed=client.get('/api/admin/assignments?scope=main',headers=headers)
    ids=[row['assignment_id'] for row in listed.json()['items']]
    changed=client.post('/api/admin/assignments/status',headers=headers,json={'scope':'main','assignment_ids':ids,'active':False})
    assert changed.status_code==200
    payload={'scope':'main','confirm_phrase':'DELETE INACTIVE LINKS'}
    deleted=client.post('/api/admin/assignments/delete-inactive',headers=headers,json=payload)
    assert deleted.status_code==200 and deleted.json()['deleted_count']==205
    replay=client.post('/api/admin/assignments/delete-inactive',headers=headers,json=payload)
    assert replay.status_code==200 and replay.json()['deleted_count']==0
    with main.conn() as connection:
        assert connection.execute('SELECT COUNT(*) AS n FROM participants').fetchone()['n']==205
        assert connection.execute('SELECT COUNT(*) AS n FROM study_participations').fetchone()['n']==205
