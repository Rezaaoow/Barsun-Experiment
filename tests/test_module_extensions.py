from test_workflow import client, create_workflow_assignment
from module_extension_checks import check_module_flow
import barsan_experiment as main


def test_five_response_dd_rest_idempotency_reentry_and_export(client):
    check_module_flow(client)


def test_new_palette_does_not_change_existing_link_snapshots(client):
    _, identity = create_workflow_assignment(client, [{'step_id':'original', 'module_code':'dd_lifetrade'}])
    saved = client.post('/api/admin/workflow', headers={'X-Admin-Token': main.ADMIN_TOKEN}, json={'steps':[
        {'step_id':'new-dd','module_code':'dd_standalone'}, {'step_id':'new-rest','module_code':'intermission'}]})
    assert saved.status_code == 200
    identified = client.post('/api/experiment/identify', json=identity)
    assert [row['module_code'] for row in identified.json()['workflow_steps']] == ['dd_lifetrade']
    assert client.post('/api/experiment/stages/start', json={**identity,'stage_code':'intermission'}).status_code == 422
    html = client.get('/admin/links').text
    assert 'data-add-module="dd_standalone"' in html and 'data-add-module="intermission"' in html


def test_module_builder_uses_same_local_font_without_external_requests(client):
    response = client.get('/admin/links')
    assert response.status_code == 200
    assert 'fonts.googleapis.com' not in response.text
    assert 'fonts.gstatic.com' not in response.text
    assert '__BARSAN_FONT_URL__' not in response.text
    assert '/assets/vazirmatn-v33.003.woff2' in response.text
    assert client.get('/assets/vazirmatn-v33.003.woff2').status_code == 200
