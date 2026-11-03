"""Shared SQLite/PostgreSQL acceptance for the five-response DD/rest workflow."""
import csv
import io
import json
import uuid
from urllib.parse import parse_qs, urlparse
import barsan_experiment as main


def create_assignment(client, steps, code=None):
    code = code or 'MODULE-' + uuid.uuid4().hex[:16]
    response = client.post('/api/admin/assignments', headers={'X-Admin-Token': main.ADMIN_TOKEN},
                           json={'assignments': [{'participant_code': code}], 'steps': steps})
    assert response.status_code == 200, response.text
    link = response.json()['assignments'][0]
    identity = dict(invitation_token=parse_qs(urlparse(link['invitation_url']).query)['invite'][0], participant_code=code)
    response = client.post('/api/experiment/consent', json={**identity, 'accepted': True})
    assert response.status_code == 200, response.text
    return identity


def start(client, identity, step, stage='dd_lifetrade', request_id=None):
    payload = dict(identity, step_id=step, stage_code=stage, start_request_id=request_id or str(uuid.uuid4()))
    response = client.post('/api/experiment/stages/start', json=payload)
    assert response.status_code == 200, response.text
    return response.json(), payload


def dd_rows(run):
    return [dict(client_event_id=str(uuid.uuid4()), row={
        'session_id': run, 'task': 'KT', 'magnitude': '100M', 'trial_in_block': n,
        'user_choice_type': 'Now', 'reaction_time_ms': 234,
        # Deliberately incorrect client labels must not override trusted export context.
        'workflow_module_code': 'forged', 'workflow_step_id': 'forged',
    }) for n in range(1, 6)]


def check_module_flow(client):
    steps = [dict(step_id='dd-one', module_code='dd_standalone'),
             dict(step_id='rest-one', module_code='intermission'),
             dict(step_id='dd-two', module_code='dd_standalone')]
    identity = create_assignment(client, steps)
    info = client.post('/api/experiment/identify', json=identity).json()
    assert [s['module_code'] for s in info['workflow_steps']] == [s['module_code'] for s in steps]
    public = client.get('/api/experiment/workflow', params={'invite': identity['invitation_token']})
    assert public.status_code == 200, public.text
    blocked = client.post('/api/experiment/stages/start', json={**identity, 'step_id': 'rest-one', 'stage_code': 'intermission'})
    assert blocked.status_code == 409
    first, payload = start(client, identity, 'dd-one')
    assert first['draft_payload']['dd_design'] == main.DD_WORKFLOW_DESIGNS['dd_standalone']
    assert client.post('/api/experiment/stages/start', json=payload).json()['run_id'] == first['run_id']
    completion = dict(identity, stage_code='dd_lifetrade', step_id='dd-one', run_id=first['run_id'])
    for endpoint in ['/api/experiment/dd/complete', '/api/experiment/stages/complete']:
        assert client.post(endpoint, json=completion).status_code == 422
    assert client.post('/api/experiment/stages/complete', json={**completion, 'stage_code': 'intermission', 'final_payload': {'continue_clicked_at': main.now()}}).status_code == 422
    rows = dd_rows(first['run_id'])
    bad = [*rows[:4], {**rows[4], 'row': {**rows[4]['row'], 'task': 'LT'}}]
    assert client.post('/api/trials', json={'session_id': first['run_id'], 'trials': bad}).status_code == 422
    assert client.post('/api/trials', json={'session_id': first['run_id'], 'trials': rows}).json()['inserted_count'] == 5
    # Commit succeeded, acknowledgement lost, same batch retried.
    replay = client.post('/api/trials', json={'session_id': first['run_id'], 'trials': rows})
    assert replay.json()['inserted_count'] == 0 and replay.json()['duplicate_count'] == 5
    for _ in range(2):
        response = client.post('/api/experiment/dd/complete', json=completion)
        assert response.status_code == 200, response.text
    rest, rest_payload = start(client, identity, 'rest-one', 'intermission')
    assert client.post('/api/experiment/stages/start', json=rest_payload).json()['run_id'] == rest['run_id']
    rest_complete = dict(identity, stage_code='intermission', step_id='rest-one', run_id=rest['run_id'])
    assert client.post('/api/experiment/stages/complete', json=rest_complete).status_code == 422
    checkpoint = dict(run_id=rest['run_id'], clock_id=str(uuid.uuid4()), step_id='rest-one', stage_code='intermission',
                      sequence=1, visible_ms=12000, uncertain_ms=0, closed=True)
    rest_complete.update(final_payload={'continue_clicked_at': main.now()}, timing_checkpoints=[checkpoint])
    for _ in range(2):
        response = client.post('/api/experiment/stages/complete', json=rest_complete)
        assert response.status_code == 200, response.text
        assert response.json()['acknowledged_checkpoints'] == [{'run_id': rest['run_id'], 'sequence': 1}]
    second, _ = start(client, identity, 'dd-two')
    assert second['run_id'] != first['run_id']
    assert client.post('/api/trials', json={'session_id': second['run_id'], 'trials': dd_rows(second['run_id'])[:1]}).json()['inserted_count'] == 1
    restarted, _ = start(client, identity, 'dd-two')
    assert restarted['run_id'] != second['run_id']
    marker = '%s' if main.DATABASE_URL else '?'
    with main.conn() as connection:
        old = connection.execute('SELECT status FROM instrument_runs WHERE run_id='+marker, (second['run_id'],)).fetchone()
        assert old['status'] == 'superseded'
        assert connection.execute('SELECT COUNT(*) AS n FROM lifetrade_trials WHERE run_id='+marker, (second['run_id'],)).fetchone()['n'] == 1
        assert connection.execute('SELECT COUNT(*) AS n FROM lifetrade_trials WHERE run_id='+marker, (restarted['run_id'],)).fetchone()['n'] == 0
        assert connection.execute("SELECT COUNT(*) AS n FROM experiment_stage_progress WHERE stage_code='intermission'").fetchone()['n'] == 0
        clock = connection.execute('SELECT visible_ms,closed FROM experiment_client_clocks WHERE run_id='+marker, (rest['run_id'],)).fetchone()
        assert clock['visible_ms'] == 12000 and clock['closed'] == 1
    exported = client.get('/api/admin/dd/records.csv', headers={'X-Admin-Token': main.ADMIN_TOKEN})
    assert exported.status_code == 200, exported.text
    selected = [row for row in csv.DictReader(io.StringIO(exported.text)) if row['run_id'] == first['run_id']]
    assert len(selected) == 5
    assert all(row['workflow_module_code'] == 'dd_standalone' and row['workflow_step_id'] == 'dd-one' for row in selected)
    assert json.loads(selected[0]['record_json'])['design']['workflow_module_code'] == 'dd_standalone'
    return identity, first, rest
