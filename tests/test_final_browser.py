import json
from test_browser_resilience import browser_env
from resilience_checks import setup_module_run


def test_visible_clock_survives_95_seconds_offline_and_deduplicates(browser_env):
    client,page,context,base=browser_env
    identity,started,step=setup_module_run(client,'ultimatum')
    info=client.post('/api/experiment/identify',json=identity).json()
    page.clock.install()
    page.evaluate('''async data=>{
      Object.assign(workflow,{...data.identity,participantCode:data.identity.participant_code,
        invitationToken:data.identity.invitation_token,assignmentId:data.assignment});
      await BarsanClock.start(data.assignment,data.run,data.step,'ultimatum');
    }''',dict(identity=identity,assignment=info['assignment_id'],run=started['run_id'],step=step))
    context.set_offline(True)
    page.clock.run_for(95000)
    page.evaluate('async()=>await BarsanClock.pause()')
    snapshot=page.evaluate('()=>BarsanClock.snapshot()')
    assert 95000<=snapshot['visible_ms']<96000 and snapshot['uncertain_ms']==0
    page.clock.run_for(65000)
    page.evaluate('async()=>await BarsanClock.close()')
    assert page.evaluate('()=>BarsanClock.snapshot().visible_ms')==snapshot['visible_ms']
    pending=page.evaluate('async()=>await BarsanClock.pending(workflow.assignmentId)')
    assert len(pending)==1 and pending[0]['value']['closed']
    context.set_offline(False)
    page.reload()
    page.evaluate('''async data=>{
      Object.assign(workflow,{participantCode:data.identity.participant_code,
        invitationToken:data.identity.invitation_token,assignmentId:data.assignment});
      await BarsanClock.flush();
    }''',dict(identity=identity,assignment=info['assignment_id']))
    assert page.evaluate('async()=> (await BarsanClock.pending(workflow.assignmentId)).length')==0
    # Replayed last checkpoint is accepted with no extra duration.
    row=pending[0]['value'];row.pop('assignment_id')
    response=client.post('/api/experiment/time/checkpoints',json={**identity,'checkpoints':[row]})
    assert response.status_code==200


def test_foreign_link_and_rejected_legacy_queue_are_retained_without_blocking(browser_env):
    _,page,_,_=browser_env
    result=page.evaluate('''async()=>{
      workflow.participantCode='SYNTHETIC';workflow.assignmentId='current-assignment';workflow.invitationToken='synthetic';
      const scope=BarsanStore.scope('ut','SYNTHETIC','old-run');
      const foreign=await BarsanStore.put(scope,'resolve:foreign',{kind:'resolve',assignment_id:'other-assignment',payload:{run_id:'old-run',round_id:'foreign'}});
      const legacy=await BarsanStore.put(scope,'resolve:legacy',{kind:'resolve',payload:{run_id:'old-run',round_id:'legacy'}});
      const original=apiWithRetry;let sends=0;
      apiWithRetry=async()=>{sends++;throw Object.assign(new Error('old run'),{status:409});};
      await recoverOrphanedUltimatumOutbox();await recoverOrphanedUltimatumOutbox();
      apiWithRetry=original;
      const rows=await BarsanStore.list(scope);
      return {sends,retained:rows.length,held:await recoveryIsHeld(legacy),foreign:await recoveryIsHeld(foreign)};
    }''')
    assert result==dict(sends=1,retained=2,held=True,foreign=True)
