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


def test_pagehide_journal_recovers_when_idb_commit_cannot_finish(browser_env):
    client,page,_,_=browser_env
    identity,started,step=setup_module_run(client,'ultimatum')
    assignment=client.post('/api/experiment/identify',json=identity).json()['assignment_id']
    page.clock.install()
    page.evaluate('''async data=>{
      Object.assign(workflow,{participantCode:data.identity.participant_code,invitationToken:data.identity.invitation_token,assignmentId:data.assignment});
      await BarsanClock.start(data.assignment,data.run,data.step,'ultimatum');await BarsanClock.flush();
    }''',dict(identity=identity,assignment=assignment,run=started['run_id'],step=step))
    page.clock.run_for(7500)
    journal=page.evaluate('''()=>{
      globalThis.BarsanStore={...BarsanStore,put:()=>new Promise(()=>{})};
      window.dispatchEvent(new Event('pagehide'));
      return JSON.parse(localStorage.getItem('barsan_clock_journal_v2_'+BarsanClock.currentRun()));
    }''')
    assert 7500<=journal['visible_ms']<9500
    page.reload()
    recovered=page.evaluate('async assignment=>await BarsanClock.pending(assignment)',assignment)
    assert len(recovered)==1 and recovered[0]['value']['visible_ms']==journal['visible_ms']
    page.evaluate('''async data=>{
      Object.assign(workflow,{participantCode:data.identity.participant_code,invitationToken:data.identity.invitation_token,assignmentId:data.assignment});
      await BarsanClock.flush();
    }''',dict(identity=identity,assignment=assignment))
    assert page.evaluate('async assignment=>(await BarsanClock.pending(assignment)).length',assignment)==0


def test_long_browser_freeze_is_marked_uncertain(browser_env):
    client,page,_,_=browser_env
    identity,started,step=setup_module_run(client,'dd_lifetrade')
    assignment=client.post('/api/experiment/identify',json=identity).json()['assignment_id']
    page.clock.install()
    page.evaluate("async data=>await BarsanClock.start(data.assignment,data.run,data.step,'dd_lifetrade')",dict(assignment=assignment,run=started['run_id'],step=step))
    page.clock.fast_forward(90000)
    page.evaluate('async()=>await BarsanClock.close()')
    row=page.evaluate('()=>BarsanClock.snapshot()')
    assert row['visible_ms']>=90000 and row['uncertain_ms']>=90000


def test_late_ack_cannot_delete_a_recreated_queue_entry(browser_env):
    _,page,_,_=browser_env
    result=page.evaluate('''async()=>{
      const scope=BarsanStore.scope('test','late-ack');
      const old=await BarsanStore.put(scope,'round-events',{payload:{event_timestamps:{result_screen_shown_at:'first'}}},'events');
      const newer=await BarsanStore.put(scope,'round-events',{payload:{event_timestamps:{advance_clicked_at:'second'}}},'events');
      await BarsanStore.ack([newer]);
      const recreated=await BarsanStore.put(scope,'round-events',{payload:{event_timestamps:{next_trial_clicked_at:'third'}}},'events');
      await BarsanStore.ack([old,newer]);
      const rows=await BarsanStore.list(scope);
      return {retained:rows.length,distinct:recreated.revision!==old.revision,
        timestamp:rows[0]?.value.payload.event_timestamps.next_trial_clicked_at};
    }''')
    assert result==dict(retained=1,distinct=True,timestamp='third')


def test_ut_dd_use_one_time_transport_and_keep_legacy_recovery(browser_env):
    client,page,_,_=browser_env
    identity,started,step=setup_module_run(client,'ultimatum')
    assignment=client.post('/api/experiment/identify',json=identity).json()['assignment_id']
    paths=page.evaluate('''async data=>{
      const normal=api,paths=[];api=(path,...args)=>{paths.push(path);return normal(path,...args);};
      Object.assign(workflow,{participantCode:data.identity.participant_code,invitationToken:data.identity.invitation_token,
        assignmentId:data.assignment,timerRunId:data.run,currentStepId:data.step,currentStage:'ultimatum',timeTrackingEnabled:true});
      await startStepTimer();await pauseStepTimer();await startStepTimer();await finishStepTimer();await BarsanClock.flush();
      api=normal;return paths;
    }''',dict(identity=identity,assignment=assignment,run=started['run_id'],step=step))
    assert '/api/experiment/time/checkpoints' in paths and '/api/experiment/time' not in paths


def test_rejected_old_auxiliary_queue_is_not_resent_each_round(browser_env):
    _,page,_,_=browser_env
    result=page.evaluate('''async()=>{
      workflow.participantCode='SYNTHETIC';workflow.assignmentId='current';workflow.invitationToken='synthetic';
      const oldScope=BarsanStore.scope('ut','SYNTHETIC','old');
      await BarsanStore.put(oldScope,'events:old',{kind:'events',payload:{round_id:'old',event_timestamps:{advance_clicked_at:'old'}}});
      const normal=apiWithRetry;let rejected=0,accepted=0;
      apiWithRetry=async(path,body)=>{
        if(body.run_id==='old'){rejected++;throw Object.assign(new Error('stale'),{status:409});}
        accepted++;return {saved:true};
      };
      await flushUtEvents();await flushUtEvents();
      const active=BarsanStore.scope('ut','SYNTHETIC','new');
      await BarsanStore.put(active,'events:new',{kind:'events',assignment_id:'current',payload:{round_id:'new',event_timestamps:{advance_clicked_at:'new'}}});
      await flushUtEvents();apiWithRetry=normal;
      return {rejected,accepted,oldRetained:(await BarsanStore.list(oldScope)).length,newPending:(await BarsanStore.list(active)).length};
    }''')
    assert result==dict(rejected=1,accepted=1,oldRetained=1,newPending=0)
