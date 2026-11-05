"""Counterbalancing design, idempotency and stable per-participant plans."""
from collections import Counter
from test_admin_links import client, create_group_link
from urllib.parse import urlparse,parse_qs
import uuid
import randomization as design
import barsan_experiment as main
from test_browser_resilience import browser_env


def test_full24_balanced_quartets():
    assert len(design.ORDERS)==24 and len(design.QUARTETS)==6
    for seed in ['one','two','three']:
        for cycle in range(3):
            deck=design._deck(seed,cycle)
            assert len(set(deck))==24
            for offset in range(0,24,4):
                quartet=deck[offset:offset+4]
                for position in range(4):assert set(row[position] for row in quartet)==set(design.BLOCKS)
                pairs=Counter((row[i],row[i+1]) for row in quartet for i in range(3))
                assert len(pairs)==12 and set(pairs.values())=={1}


def _claim(client,token,code):
    r=client.post('/api/experiment/group/claim',json={'group_token':token,'participant_code':code,'first_name':'Synthetic','last_name':'Counterbalance','claim_request_id':str(uuid.uuid4())})
    assert r.status_code==200,r.text
    identity={k:r.json()[k] for k in ['participant_code','invitation_token']}
    assert client.post('/api/experiment/consent',json={**identity,'first_name':'Synthetic','last_name':'Counterbalance','accepted':True}).status_code==200
    return identity


def test_real_start_full24_retry_and_restart_stable(client):
    headers={'X-Admin-Token':main.ADMIN_TOKEN}
    group=client.post('/api/admin/group-links',headers=headers,json={'scope':'test','label':'Synthetic atomic order test','steps':[{'step_id':'ut','module_code':'ultimatum_control'}]}).json()
    token=parse_qs(urlparse(group['invitation_url']).query)['group'][0];plans=[]
    for i in range(24):
        identity=_claim(client,token,'CB-'+str(i))
        body={**identity,'stage_code':'ultimatum','step_id':'ut','start_request_id':str(uuid.uuid4())}
        first=client.post('/api/experiment/stages/start',json=body);assert first.status_code==200,first.text
        first=first.json();retry=client.post('/api/experiment/stages/start',json=body).json()
        assert retry['run_id']==first['run_id'] and retry['draft_payload']==first['draft_payload']
        restarted=client.post('/api/experiment/stages/start',json={**body,'start_request_id':str(uuid.uuid4())}).json()
        assert restarted['run_id']!=first['run_id'] and restarted['draft_payload']==first['draft_payload']
        plans.append(first['draft_payload']['randomization'])
    assert len({tuple(p['block_order']) for p in plans})==24
    assert [p['ticket'] for p in plans]==list(range(24))
    assert all(p['stratum']=='Control' for p in plans)
    with main.conn() as connection:
        assert connection.execute('SELECT count(*) AS n FROM experiment_randomization_allocations').fetchone()['n']==24
        assert connection.execute('SELECT next_ticket FROM experiment_randomization_streams').fetchone()['next_ticket']==24


def test_dd_positions_stable_and_balanced(client):
    group=client.post('/api/admin/group-links',headers={'X-Admin-Token':main.ADMIN_TOKEN},json={'scope':'test','label':'Synthetic DD plan','steps':[{'step_id':'dd','module_code':'dd_standalone'}]}).json()
    token=parse_qs(urlparse(group['invitation_url']).query)['group'][0]
    identity=_claim(client,token,'CB-DD')
    body={**identity,'stage_code':'dd_lifetrade','step_id':'dd','start_request_id':str(uuid.uuid4())}
    first=client.post('/api/experiment/stages/start',json=body).json()
    second=client.post('/api/experiment/stages/start',json={**body,'start_request_id':str(uuid.uuid4())}).json()
    positions=first['draft_payload']['dd_positions']['now_left']
    assert len(positions)==5 and sum(positions) in (2,3)
    assert positions==second['draft_payload']['dd_positions']['now_left']


def test_closed_indexeddb_handle_recovers_without_refresh(browser_env):
    _,page,_,_=browser_env
    result=page.evaluate("""async()=>{
      const original=indexedDB.open.bind(indexedDB);let captured;
      indexedDB.open=function(...args){const request=original(...args);request.addEventListener('success',()=>{captured=request.result});return request;};
      const scope=BarsanStore.scope('test','stale-handle');
      await BarsanStore.put(scope,'saved',{answer:'preserved'});
      captured.close();
      await BarsanStore.put(scope,'after-close',{answer:'new'});
      const rows=await BarsanStore.list(scope);
      return rows.map(row=>row.value.answer).sort();
    }""")
    assert result==['new','preserved']


def test_retry_timer_survives_prescribed_wait_and_repeated_outage(browser_env):
    client,page,_,_=browser_env
    from resilience_checks import setup_module_run
    identity,started,_=setup_module_run(client,'ultimatum')
    page.evaluate('''args=>{
      workflow.participantCode='TEST';workflow.invitationToken=args.token;
      ultimatumState={runId:args.run,results:[],score:0,practiceScore:0,totalPotentialLost:0,currentEventTimestamps:{},resolveInFlight:false,proposalSubmitting:false};
    }''',{'run':started['run_id'],'token':identity['invitation_token']})
    result=page.evaluate('''async()=>{
      await BarsanStore.initialize();
      const round=await utPrepare('practice','practice_proposer_agent',0,1,0);
      ultimatumState.currentRound=round;ultimatumState.decisionScreenShownAt=nowISO();
      const normalSend=utSendOutboxItem;let attempts=0;
      utSendOutboxItem=async(...args)=>{
        attempts++;
        if(attempts<=3){scheduleUtOutboxRetry();throw Object.assign(new Error('Synthetic outage'),{network:true,status:0});}
        return normalSend(...args);
      };
      utWaitMs=()=>1200;
      await utStateSubmitProposal(4,true,'practice_proposer_agent',0,1,300,nowISO());
      const deadline=Date.now()+20000;
      while(!ultimatumState.results.length&&Date.now()<deadline)await new Promise(resolve=>setTimeout(resolve,50));
      return {attempts,results:ultimatumState.results.length,
        pending:(await utReadOutboxKey(utOutboxKey(ultimatumState.runId))).filter(row=>row.kind==='resolve').length};
    }''')
    assert result['attempts']>=4 and result['results']==1 and result['pending']==0


def test_concurrent_new_allocations_and_duplicate_requests(client):
    from counterbalance_checks import check_atomic_counterbalancing
    check_atomic_counterbalancing(main,client)
