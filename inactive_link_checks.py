"""Regression shared by SQLite, PostgreSQL and a strictly isolated Staging study."""
import json
import uuid
from urllib.parse import parse_qs,urlparse
import barsan_experiment as main


def check_inactive_link_deletion(client):
    headers={'X-Admin-Token':main.ADMIN_TOKEN}
    step='inactive-fix-dd'
    group=client.post('/api/admin/group-links',headers=headers,json={'scope':'main','label':'Synthetic inactive-link regression',
        'steps':[{'step_id':step,'module_code':'dd_standalone'}]})
    assert group.status_code==200,group.text
    token=parse_qs(urlparse(group.json()['invitation_url']).query)['group'][0]
    claim={'group_token':token,'assign_random':True,'claim_request_id':str(uuid.uuid4())}
    result=client.post('/api/experiment/group/claim',json=claim)
    assert result.status_code==200,result.text
    owned=result.json();identity={k:owned[k] for k in ['invitation_token','participant_code']}
    assert client.post('/api/experiment/consent',json={**identity,'accepted':True}).status_code==200
    start=client.post('/api/experiment/stages/start',json={**identity,'stage_code':'dd_lifetrade','step_id':step,'start_request_id':str(uuid.uuid4())})
    assert start.status_code==200,start.text
    run=start.json()['run_id'];marker='%s' if main.DATABASE_URL else '?'
    with main.conn() as connection:
        assignment=main._lookup_assignment(connection,identity['invitation_token'],identity['participant_code'])
    event=client.post('/api/experiment/time',json={**identity,'stage_code':'dd_lifetrade','step_id':step,'event':'start','event_id':str(uuid.uuid4())})
    assert event.status_code==200,event.text
    rows=[{'client_event_id':str(uuid.uuid4()),'row':{'task':'KT','magnitude':'100M','trial_in_block':n,'user_choice_type':'Later'}} for n in range(1,6)]
    assert client.post('/api/trials',json={'session_id':run,'trials':rows}).status_code==200
    checkpoint=dict(run_id=run,clock_id=str(uuid.uuid4()),step_id=step,stage_code='dd_lifetrade',sequence=1,visible_ms=12345,uncertain_ms=0,closed=True)
    completed=client.post('/api/experiment/dd/complete',json={**identity,'stage_code':'dd_lifetrade','step_id':step,'run_id':run,'timing_checkpoints':[checkpoint]})
    assert completed.status_code==200,completed.text
    active=client.post('/api/experiment/group/claim',json={**claim,'claim_request_id':str(uuid.uuid4())})
    assert active.status_code==200
    active_identity={k:active.json()[k] for k in ['invitation_token','participant_code']}
    outside=client.post('/api/admin/group-links',headers=headers,json={'scope':'test','label':'Synthetic scope sentinel','steps':[{'step_id':step,'module_code':'dd_standalone'}]})
    outside_token=parse_qs(urlparse(outside.json()['invitation_url']).query)['group'][0]
    other=client.post('/api/experiment/group/claim',json={'group_token':outside_token,'assign_random':True,'claim_request_id':str(uuid.uuid4())})
    assert other.status_code==200
    with main.conn() as connection:
        other_assignment=main._lookup_assignment(connection,other.json()['invitation_token'],other.json()['participant_code'])
        active_assignment=main._lookup_assignment(connection,active_identity['invitation_token'],active_identity['participant_code'])
        for target in [assignment['assignment_id'],other_assignment['assignment_id']]:
            connection.execute('UPDATE experiment_assignments SET active=0 WHERE assignment_id='+marker,(target,))
        before={table:connection.execute('SELECT COUNT(*) AS n FROM '+table).fetchone()['n'] for table in ['participants','study_participations','instrument_runs','lifetrade_trials','ultimatum_trials','questionnaire_responses','experiment_client_clocks','experiment_client_clock_checkpoints']}
        # Confirm all FK paths that previously blocked deletion really exist.
        for table in ['experiment_consents','experiment_workflow_progress','experiment_time_tracking','experiment_stage_start_requests','experiment_time_event_requests','experiment_group_claim_requests']:
            assert connection.execute('SELECT COUNT(*) AS n FROM '+table+' WHERE assignment_id='+marker,(assignment['assignment_id'],)).fetchone()['n']>=1
    bad=client.post('/api/admin/assignments/delete-inactive',headers=headers,json={'scope':'main','confirm_phrase':'incorrect'})
    assert bad.status_code==422
    deleted=client.post('/api/admin/assignments/delete-inactive',headers=headers,json={'scope':'main','confirm_phrase':'DELETE INACTIVE LINKS'})
    assert deleted.status_code==200,deleted.text
    assert deleted.json()['deleted_count']>=1
    assert client.post('/api/experiment/group/claim',json=claim).status_code==409
    with main.conn() as connection:
        after={table:connection.execute('SELECT COUNT(*) AS n FROM '+table).fetchone()['n'] for table in before}
        assert after==before
        assert connection.execute('SELECT 1 FROM experiment_assignments WHERE assignment_id='+marker,(assignment['assignment_id'],)).fetchone() is None
        for preserved in [other_assignment['assignment_id'],active_assignment['assignment_id']]:
            assert connection.execute('SELECT 1 FROM experiment_assignments WHERE assignment_id='+marker,(preserved,)).fetchone() is not None
        tombstone=connection.execute('SELECT assignment_id FROM experiment_group_claim_requests WHERE request_id='+marker,(claim['claim_request_id'],)).fetchone()
        assert tombstone is not None and tombstone['assignment_id'] is None
        for table in ['experiment_consents','experiment_workflow_progress','experiment_time_tracking','experiment_stage_start_requests','experiment_time_event_requests']:
            assert connection.execute('SELECT COUNT(*) AS n FROM '+table+' WHERE assignment_id='+marker,(assignment['assignment_id'],)).fetchone()['n']==0
    with main.conn() as connection:
        for target in [active_assignment['assignment_id'],other_assignment['assignment_id']]:
            connection.execute('UPDATE experiment_assignments SET active=0 WHERE assignment_id='+marker,(target,))
        for target in [group.json()['group_link_id'],outside.json()['group_link_id']]:
            connection.execute('UPDATE experiment_group_links SET active=0 WHERE group_link_id='+marker,(target,))
    return {'deleted_test_invitation':True,'all_six_foreign_key_paths_covered':True,'research_counts_unchanged':True,
            'active_link_preserved':True,'other_scope_preserved':True,'group_retry_rejected':True,'no_real_study_targeted':main.STUDY_CODE.startswith('barsan-test-')}
