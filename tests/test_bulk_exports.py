from test_task_resilience import client
import barsan_experiment as main
from bulk_exports import take_slot


def test_only_authenticated_exports_queue_and_participants_stay_available(client):
    slot=take_slot(main.ROOT+'/.bulk-export.lock')
    assert slot is not None
    try:
        for path in ['/api/admin/dd/records.csv','/api/admin/ultimatum/records.csv','/api/admin/time/checkpoints.csv','/api/admin/export-all.zip']:
            response=client.get(path,headers={'X-Admin-Token':main.ADMIN_TOKEN})
            assert response.status_code==429
            assert response.headers['Retry-After']=='2' and response.headers['X-Barsan-Export-Busy']=='1'
        assert client.get('/api/admin/dd/records.csv').status_code==401
        assert client.get('/api/health').status_code==200
    finally:
        slot.close()
    assert client.get('/api/admin/dd/records.csv',headers={'X-Admin-Token':main.ADMIN_TOKEN}).status_code==200
    # Even an unknown route releases its lease when the response ends.
    assert client.get('/api/admin/not-installed.csv',headers={'X-Admin-Token':main.ADMIN_TOKEN}).status_code==404
    next_slot=take_slot(main.ROOT+'/.bulk-export.lock')
    assert next_slot is not None
    next_slot.close()


def test_recent_task_activity_defers_bulk_reads_but_not_health(client,monkeypatch):
    from resilience_checks import setup_module_run
    monkeypatch.setattr(main,'EXPORT_IDLE_SECONDS',90)
    assert not main._recent_task_activity()
    setup_module_run(client,'dd_lifetrade')
    assert main._recent_task_activity()
    response=client.get('/api/admin/dd/records.csv',headers={'X-Admin-Token':main.ADMIN_TOKEN})
    assert response.status_code==429 and response.headers['X-Barsan-Export-Busy']=='1'
    assert client.get('/api/health').status_code==200
    monkeypatch.setattr(main,'EXPORT_IDLE_SECONDS',0)
    assert client.get('/api/admin/dd/records.csv',headers={'X-Admin-Token':main.ADMIN_TOKEN}).status_code==200


def test_export_admission_failure_defers_download_without_holding_slot(client,monkeypatch):
    def unavailable():
        raise RuntimeError('Synthetic admission check failure')
    monkeypatch.setattr(main,'_recent_task_activity',unavailable)
    response=client.get('/api/admin/dd/records.csv',headers={'X-Admin-Token':main.ADMIN_TOKEN})
    assert response.status_code==429 and response.headers['X-Barsan-Export-Busy']=='1'
    assert client.get('/api/health').status_code==200
    slot=take_slot(main.ROOT+'/.bulk-export.lock')
    assert slot is not None
    slot.close()
