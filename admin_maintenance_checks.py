"""Shared SQLite/PostgreSQL checks; live use requires an isolated synthetic STUDY_CODE."""
import uuid
from urllib.parse import parse_qs, urlparse
import barsan_experiment as main


def check_admin_maintenance(client, *, disposable_database=True):
    if not disposable_database:
        assert main.STUDY_CODE.startswith('barsan-test-admin-repair-')
    headers={'X-Admin-Token':main.ADMIN_TOKEN}
    marker='%s' if main.DATABASE_URL else '?'
    def post(path,payload,expected=200):
        r=client.post('/api/'+path,headers=headers,json=payload)
        assert r.status_code==expected,(path,r.status_code)
        return r.json()
    def group(scope='main'):
        return post('admin/group-links',{'scope':scope,'label':'Synthetic admin maintenance check','steps':[{'step_id':'admin-fix-dd','module_code':'dd_standalone'}]})
    def claim(g):
        token=parse_qs(urlparse(g['invitation_url']).query)['group'][0]
        request={"participant_code":"TEST-"+uuid.uuid4().hex, "first_name":"Synthetic", "last_name":"Participant", 'group_token':token,'assign_random':False,'claim_request_id':str(uuid.uuid4())}
        return post('experiment/group/claim',request),request
    def identity(row):return {k:row[k] for k in ['invitation_token','participant_code']}
    def identify(row,expected=200):return post('experiment/identify',identity(row),expected)
    first=group();a,claim_a=claim(first);b,claim_b=claim(first)
    for row in [a,b]:post('experiment/consent',{"first_name":"Synthetic", "last_name":"Participant", **identity(row),'accepted':True})
    info_a=identify(a);info_b=identify(b);a_id=info_a['assignment_id'];b_id=info_b['assignment_id']
    post('experiment/consent',{"first_name":"Synthetic", "last_name":"Participant", **identity(a),'accepted':True})
    started=post('experiment/stages/start',{**identity(a),'stage_code':'dd_lifetrade','step_id':'admin-fix-dd','start_request_id':str(uuid.uuid4())})
    run=started['run_id']
    post('experiment/time',{**identity(a),'stage_code':'dd_lifetrade','step_id':'admin-fix-dd','event':'start','event_id':str(uuid.uuid4())})
    post('trials',{'session_id':run,'trials':[{'client_event_id':str(uuid.uuid4()),'row':{'task':'KT','magnitude':'100M','trial_in_block':n,'user_choice_type':'Later'}} for n in range(1,6)]})
    checkpoint=dict(run_id=run,clock_id=str(uuid.uuid4()),step_id='admin-fix-dd',stage_code='dd_lifetrade',sequence=1,visible_ms=1234,uncertain_ms=0,closed=True)
    post('experiment/dd/complete',{**identity(a),'stage_code':'dd_lifetrade','step_id':'admin-fix-dd','run_id':run,'timing_checkpoints':[checkpoint]})
    outside=group('test')
    before_outside=next(item for item in client.get('/api/admin/group-links?scope=test',headers=headers).json()['items'] if item['group_link_id']==outside['group_link_id'])
    # Whole selection validation: a bad/stale ID cannot partly disable valid rows.
    post('admin/assignments/status',{'scope':'main','assignment_ids':[a_id,'missing-synthetic-id'],'active':False},404)
    identify(a)
    post('admin/assignments/status',{'scope':'main','assignment_ids':[a_id,a_id],'active':False},422)
    post('admin/group-links/status',{'scope':'main','group_link_ids':[outside['group_link_id']],'active':False},404)
    post('admin/group-links/status',{'scope':'main','group_link_ids':[first['group_link_id']],'active':False})
    post('experiment/group/claim',{**claim_a,'claim_request_id':str(uuid.uuid4())},404)
    identify(a)  # Closing entry alone preserves personal invitations.
    result=post('admin/group-links/status',{'scope':'main','group_link_ids':[first['group_link_id']],'active':False,'include_assignments':True})
    assert result['assignments_deactivated']==2
    identify(a,403);identify(b,403)
    post('admin/assignments/status',{'scope':'main','assignment_ids':[a_id],'active':True})
    identify(a)
    selected=post('admin/assignments/delete-inactive',{'scope':'main','assignment_ids':[a_id,b_id],'confirm_phrase':'DELETE INACTIVE LINKS'})
    assert selected['deleted_count']==1
    identify(a);identify(b,403)
    with main.conn() as c:
        assert c.execute('SELECT assignment_id FROM experiment_group_claim_requests WHERE request_id='+marker,(claim_b['claim_request_id'],)).fetchone()['assignment_id'] is None
    post('admin/group-links/delete',{'scope':'main','group_link_ids':[first['group_link_id']],'confirm_phrase':'wrong'},422)
    deleted=post('admin/group-links/delete',{'scope':'main','group_link_ids':[first['group_link_id'],outside['group_link_id']],'confirm_phrase':'DELETE GROUP LINKS'})
    assert deleted['deleted_count']==1 and deleted['preserved_assignments']==1
    assert post('admin/group-links/delete',{'scope':'main','group_link_ids':[first['group_link_id']],'confirm_phrase':'DELETE GROUP LINKS'})['deleted_count']==0
    identify(a)
    post('admin/group-links/status',{'scope':'main','group_link_ids':[first['group_link_id']],'active':True},404)
    post('experiment/group/claim',claim_a,404)
    with main.conn() as c:
        assert c.execute('SELECT COUNT(*) AS n FROM lifetrade_trials WHERE run_id='+marker,(run,)).fetchone()['n']==5
        assert c.execute('SELECT group_link_id FROM experiment_assignments WHERE assignment_id='+marker,(a_id,)).fetchone()['group_link_id']==first['group_link_id']
    second=group();claim(second)  # A live claim receipt must be removed before this group.
    post('admin/maintenance/reset',{'scope':'main','confirm_phrase':'incorrect'},422)
    preview=client.get('/api/admin/maintenance/preview?scope=main',headers=headers)
    assert preview.status_code==200 and preview.json()['counts']['lifetrade_trials']>=5
    cleared=post('admin/maintenance/reset',{'scope':'main','confirm_phrase':'CLEAR MAIN DATA'})
    assert cleared['reset'] is True
    for name in ['experiment_stage_start_requests','experiment_time_event_requests','experiment_group_claim_requests','experiment_client_clock_checkpoints','experiment_client_clocks']:
        assert cleared['deleted_rows'][name]>=1,name
    after=client.get('/api/admin/maintenance/preview?scope=main',headers=headers).json()['counts']
    assert all(after[name]==0 for name in ['assignments','group_links','participations','runs','lifetrade_trials'])
    assert next(item for item in client.get('/api/admin/group-links?scope=test',headers=headers).json()['items'] if item['group_link_id']==outside['group_link_id'])==before_outside
    if disposable_database:
        post('admin/maintenance/reset',{'scope':'test','confirm_phrase':'CLEAR TEST DATA'})
        with main.conn() as c:
            assert c.execute("SELECT COUNT(*) AS n FROM study_workflows WHERE substr(study_code,1,12)='barsan-test-'").fetchone()['n']==0
    else:
        # Only remove our own synthetic sentinel URL, never all existing Staging test studies.
        post('admin/group-links/delete',{'scope':'test','group_link_ids':[outside['group_link_id']],'confirm_phrase':'DELETE GROUP LINKS'})
    return {'passed':True,'reset_with_all_receipts':True,'group_delete_preserves_research':True,'individual_status_and_selected_delete':True,'group_disable_modes':True,'scope_isolation':True}
