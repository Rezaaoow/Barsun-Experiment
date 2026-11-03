"""Real IndexedDB and task JavaScript, not a memory-store approximation."""
import json
import os
import socket
import threading
import time
from pathlib import Path

import httpx
import pytest
import uvicorn
from playwright.sync_api import sync_playwright

import barsan_experiment as main
from resilience_checks import setup_module_run

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def browser_env(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DB_PATH", str(tmp_path / "browser.sqlite3"))
    monkeypatch.setattr(main, "ADMIN_TOKEN", "browser-synthetic-admin")
    monkeypatch.setattr(main, "INVITATION_TOKEN_KEY_FILE", str(tmp_path / "test.key"))
    main.init_db()
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(main.app, host="127.0.0.1", port=port, log_level="error", access_log=False))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}"
    with httpx.Client(base_url=base, timeout=10) as client:
        for _ in range(100):
            try:
                if client.get("/api/health").status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(.05)
        else:
            raise AssertionError("Disposable browser server did not become ready")
        with sync_playwright() as pw:
            executable = os.environ.get("BARSAN_BROWSER_EXECUTABLE")
            browser = pw.chromium.launch(headless=True, executable_path=executable or None)
            context = browser.new_context()
            # The test does not depend on external fonts/network availability.
            context.route("https://cdn.jsdelivr.net/**", lambda route: route.abort())
            page = context.new_page()
            page.goto(base)
            page.wait_for_function("typeof BarsanStore!=='undefined'")
            try:
                yield client, page, context, base
            finally:
                context.close()
                browser.close()
    server.should_exit = True
    thread.join(timeout=5)


def test_real_indexeddb_migration_reload_revision_and_abort(browser_env):
    _, page, context, base = browser_env
    result = page.evaluate("""async()=>{
      await BarsanStore.initialize();
      const scope=BarsanStore.scope('test','revision');
      const first=await BarsanStore.put(scope,'events',{payload:{wait_duration_ms:2017,event_timestamps:{advance_clicked_at:'first'}}},'events');
      await BarsanStore.put(scope,'events',{payload:{wait_duration_ms:2018,event_timestamps:{result_screen_shown_at:'shown',advance_clicked_at:'second'}}},'events');
      await BarsanStore.ack([first]);
      const pending=await BarsanStore.list(scope);
      localStorage.setItem('legacy-dd',JSON.stringify([{client_event_id:'legacy-choice',extra:{kept:true}}]));
      await BarsanStore.migrate('legacy-dd',BarsanStore.scope('dd','legacy-run'),row=>({key:row.client_event_id,value:row}));
      localStorage.setItem('bad-legacy','{invalid');
      let rejected=false;
      try{await BarsanStore.migrate('bad-legacy',scope,row=>({key:'bad',value:row}));}catch(error){rejected=true;}
      return {pending:pending.length,wait:pending[0].value.payload.wait_duration_ms,events:pending[0].value.payload.event_timestamps,
        legacyRemoved:localStorage.getItem('legacy-dd')===null,invalidPreserved:rejected&&localStorage.getItem('bad-legacy')==='{invalid'};
    }""")
    assert result == {"pending": 1, "wait": 2017, "events": {"advance_clicked_at": "first", "result_screen_shown_at": "shown"},
                      "legacyRemoved": True, "invalidPreserved": True}
    page.reload()
    assert page.evaluate("async()=> (await BarsanStore.list(BarsanStore.scope('dd','legacy-run')))[0].value.extra.kept") is True
    # Abort a real IDB transaction; the previously committed answer must survive.
    assert page.evaluate("""async()=>{
      const db=await new Promise((resolve,reject)=>{const r=indexedDB.open('barsan-task-outbox-v1',1);r.onsuccess=()=>resolve(r.result);r.onerror=reject;});
      await new Promise(resolve=>{const tx=db.transaction('entries','readwrite');tx.objectStore('entries').clear();tx.onabort=resolve;tx.abort();});
      db.close();return (await BarsanStore.list(BarsanStore.scope('dd','legacy-run'))).length;
    }""") == 1
    # A second tab cannot acquire a lock held by the first tab.
    page.evaluate("async()=>await BarsanStore.acquire('lock-test')")
    second = context.new_page()
    second.goto(base)
    assert second.evaluate("async()=>{try{await BarsanStore.acquire('lock-test');return false;}catch(error){return true;}}") is True
    second.close()


def test_dd_offline_ack_loss_and_old_queue_migration(browser_env):
    client, page, context, _ = browser_env
    identity, started, _step = setup_module_run(client, "dd_lifetrade")
    run_id = started["run_id"]
    page.evaluate("""args=>{
      sessionId=args.run;participantCode='TEST';workflow.participantCode='TEST';workflow.invitationToken=args.token;
      window.utDelay=async()=>{};
      localStorage.setItem('lifetrade_pending_'+sessionId,JSON.stringify([{session_id:sessionId,client_event_id:'migrated-dd-event',custom:{kept:true}}]));
    }""", {"run": run_id, "token": identity["invitation_token"]})
    page.evaluate("async()=>{await BarsanStore.initialize();await migrateDdQueues();}")
    context.set_offline(True)
    result = page.evaluate("""async()=>{
      await saveTrial({session_id:sessionId,client_event_id:'offline-dd-event',reaction_time_ms:400});
      await flushPending();return (await readPending()).length;
    }""")
    assert result == 2
    context.set_offline(False)
    # Server commits, but the browser never sees the acknowledgement.
    page.evaluate("""()=>{
      window.__normalApi=api;let dropped=false;
      api=async(path,body,options)=>{const result=await __normalApi(path,body,options);
        if(path==='/api/trials'&&!dropped){dropped=true;throw Object.assign(new Error('Synthetic lost acknowledgement'),{network:true,status:0});}
        return result;};
    }""")
    assert page.evaluate("async()=>await flushPending()") == 0
    marker = "%s" if main.DATABASE_URL else "?"
    with main.conn() as connection:
        count = connection.execute("SELECT COUNT(*) AS n FROM lifetrade_trials WHERE run_id=" + marker, (run_id,)).fetchone()["n"]
    assert count == 2
    assert page.evaluate("()=>localStorage.getItem('lifetrade_pending_'+sessionId)") is None


def test_ultimatum_network_overlaps_wait_and_auxiliary_events_do_not_block(browser_env):
    client, page, _context, _base = browser_env
    identity, started, _step = setup_module_run(client, "ultimatum")
    run_id = started["run_id"]
    page.evaluate("""args=>{
      workflow.participantCode='TEST';workflow.invitationToken=args.token;
      ultimatumState={runId:args.run,results:[],score:0,practiceScore:0,totalPotentialLost:0,
        currentEventTimestamps:{},resolveInFlight:false,proposalSubmitting:false};
    }""", {"run": run_id, "token": identity["invitation_token"]})
    result = page.evaluate("""async()=>{
      await BarsanStore.initialize();
      const round=await utPrepare('practice','practice_proposer_agent',0,1,0);
      ultimatumState.currentRound=round;ultimatumState.decisionScreenShownAt=nowISO();
      window.__testTimeline={};window.__testAdvanced=false;utStartPracticeResponder=async()=>{__testAdvanced=true;};
      const recordEvents=utRecordRoundEvents;utRecordRoundEvents=async(...args)=>{__testTimeline.waitEnded=performance.now();return recordEvents(...args);};
      const normal=utSendOutboxItem;
      utSendOutboxItem=async(...args)=>{__testTimeline.requestStarted=performance.now();const result=await normal(...args);__testTimeline.requestEnded=performance.now();return result;};
      utWaitMs=()=>250;
      const start=performance.now();
      await utStateSubmitProposal(4,true,'practice_proposer_agent',0,1,300,nowISO());
      const shown=performance.now();
      // No need to wait for the 6–10 second telemetry batch timer.

      const normalApi=api;
      api=(path,...args)=>path.includes('/events')?new Promise(()=>{}):normalApi(path,...args);
      const button=[...document.querySelectorAll('.ut-actions button')].find(b=>b.textContent==='دور تمرینی بعدی');
      button.click();
      for(let i=0;i<40&&!__testAdvanced;i++)await new Promise(resolve=>setTimeout(resolve,20));
      api=normalApi;
      return {requestStartedBeforeWaitEnded:__testTimeline.requestStarted<__testTimeline.waitEnded,displayedAfter:shown-start,
        advanced:__testAdvanced,stored:(await utReadOutboxKey(utOutboxKey(ultimatumState.runId))).filter(row=>row.kind==='events').length,
        resultCount:ultimatumState.results.length,prefetched:Boolean(ultimatumState.prefetchedRound)};
    }""")
    assert result["requestStartedBeforeWaitEnded"] is True
    assert result["displayedAfter"] >= 240
    assert result["advanced"] is True and result["stored"] == 1
    assert result["resultCount"] == 1 and result["prefetched"] is True


def test_background_hang_cannot_block_foreground_transport(browser_env):
    _, page, _, _ = browser_env
    assert page.evaluate("""async()=>{
      BarsanTransport.run(()=>new Promise(()=>{}),'telemetry');
      return Promise.race([BarsanTransport.run(async()=>true),new Promise(resolve=>setTimeout(()=>resolve(false),200))]);
    }""") is True


def test_response_body_is_covered_by_network_deadline(browser_env):
    _, page, _, _ = browser_env
    assert page.evaluate("""async()=>{
      fetch=async(_url,options)=>({ok:true,json:()=>new Promise((_resolve,reject)=>{
        options.signal.addEventListener('abort',()=>reject(new DOMException('aborted','AbortError')));
      })});
      return Promise.race([
        performApi('/synthetic-timeout',{}, {timeoutMs:30}).then(()=>false,error=>error.network&&error.status===0),
        new Promise(resolve=>setTimeout(()=>resolve(false),300))
      ]);
    }""") is True


def test_full_client_ultimatum_uses_one_prepare_and_delivers_final_auxiliary_events(browser_env):
    client, page, _, _ = browser_env
    identity, started, step = setup_module_run(client, "ultimatum")
    result = page.evaluate("""async args=>{
      workflow.participantCode='TEST';workflow.invitationToken=args.token;workflow.conditionCode='A';
      workflow.workflowMode=true;workflow.currentStepId=args.step;workflow.currentStage='ultimatum';
      workflow.workflowSteps=[{step_id:args.step,module_code:'ultimatum'}];workflow.workflowStepIndex=0;
      workflow.testModuleOnly='ultimatum';
      ultimatumState={runId:args.run,results:[],score:0,practiceScore:0,totalPotentialLost:0,
       blockOrder:args.order,blockIndex:0,blockSummaries:{},currentEventTimestamps:{},
       resolveInFlight:false,proposalSubmitting:false,currentPhase:'practice'};
      await BarsanStore.initialize();utWaitMs=()=>5;
      const paths=[],normalFetch=fetch;
      fetch=async(url,options)=>{paths.push(new URL(url,location.href).pathname);return normalFetch(url,options);};
      const specs=[{phase:'practice',block:'practice_proposer_agent',order:0,trial:1},
       {phase:'practice',block:'practice_responder_coworker',order:0,trial:1}];
      args.order.forEach((block,index)=>{for(let trial=1;trial<=5;trial++)specs.push({phase:'main',block,order:index+1,trial});});
      for(const spec of specs){
       if(spec.phase==='main'){
        ultimatumState.blockIndex=spec.order-1;await utStartMainTrial(spec.trial);
       }else if(spec.block==='practice_proposer_agent')await utStartPracticeProposer();
       else{
        await utStartPracticeResponder();
        [...document.querySelectorAll('.ut-actions button')].find(button=>button.textContent==='آماده‌ام').click();
        for(let i=0;i<50&&!document.querySelector('.ut-actions .accept');i++)await new Promise(resolve=>setTimeout(resolve,20));
        if(!document.querySelector('.ut-actions .accept'))throw new Error('Practice responder did not become ready');
       }
       const round=ultimatumState.currentRound;
       if(round.role==='proposer'){
        await utStateSubmitProposal(4,spec.phase==='practice',spec.block,spec.order,spec.trial,312,nowISO());
       }else{
        await utPostResolve({invitation_token:args.token,participant_code:'TEST',run_id:args.run,
         round_id:round.round_id,participant_decision:'accept',reaction_time_ms:312,
         wait_duration_ms:ultimatumState.waitDurationMs,decision_screen_shown_at:ultimatumState.decisionScreenShownAt,
         responded_at:nowISO(),client_event_timestamps:ultimatumState.currentEventTimestamps},
         spec.phase==='practice',spec.block,spec.order,spec.trial);
       }
       await utRecordRoundEvents(round.round_id,{result_screen_shown_at:nowISO(),advance_clicked_at:nowISO()});
      }
      await utFinishUltimatum();
      const pending=await BarsanStore.list();
      return {rounds:ultimatumState.results.length,completed:ultimatumState.completed,
       prepares:paths.filter(path=>path.endsWith('/prepare')).length,
       resolves:paths.filter(path=>path.endsWith('/resolve')).length,
       pending:pending.filter(entry=>JSON.parse(entry.scope).includes(args.run)).length};
    }""", {"run": started["run_id"], "token": identity["invitation_token"], "step": step,
             "order": started["draft_payload"]["block_order"]})
    assert result == {"rounds": 22, "completed": True, "prepares": 1, "resolves": 22, "pending": 0}
    with main.conn() as connection:
        rows = connection.execute("SELECT payload FROM ultimatum_trials WHERE run_id=?", (started["run_id"],)).fetchall()
    assert len(rows) == 22
    assert all("advance_clicked_at" in main._json_value(row["payload"])["client_event_timestamps"] for row in rows)


def test_ultimatum_legacy_migration_offline_and_recovery_before_new_attempt(browser_env):
    client, page, context, _ = browser_env
    identity, started, step = setup_module_run(client, "ultimatum")
    prepared = client.post("/api/experiment/ultimatum/prepare", json={**identity, "run_id": started["run_id"],
        "phase": "practice", "block_code": "practice_proposer_agent", "block_order": 0,
        "trial_in_block": 1, "trial_sequence": 0})
    assert prepared.status_code == 200
    page.evaluate("""async args=>{
      workflow.participantCode='TEST';workflow.invitationToken=args.token;utDelay=async()=>{};
      const payload={run_id:args.run,round_id:args.round,participant_offer_to_opponent:4,
       invitation_token:args.token,participant_code:'TEST',prepare_next:true};
      localStorage.setItem(utOutboxPrefix()+encodeURIComponent(args.run),JSON.stringify([
       {kind:'resolve',item_id:args.round,payload,meta:{isPractice:true,blockCode:'practice_proposer_agent',blockOrder:0,trial:1}}
      ]));
      await BarsanStore.initialize();await migrateUtQueues();
    }""", {"run": started["run_id"], "round": prepared.json()["round_id"], "token": identity["invitation_token"]})
    context.set_offline(True)
    assert page.evaluate("""async args=>{
      const rows=await utReadOutboxKey(utOutboxKey(args.run));
      let failed=false;try{await utSendOutboxItem(args.run,rows[0]);}catch(error){failed=true;}
      return failed&&(await utReadOutboxKey(utOutboxKey(args.run))).length===1
       &&!JSON.stringify(await BarsanStore.list()).includes(args.token);
    }""", {"run": started["run_id"], "token": identity["invitation_token"]}) is True
    context.set_offline(False)
    page.reload()
    page.evaluate("""async args=>{
      workflow.participantCode='TEST';workflow.invitationToken=args.token;
      await recoverOrphanedUltimatumOutbox();
    }""", {"token": identity["invitation_token"]})
    restarted = client.post("/api/experiment/stages/start", json={**identity, "stage_code": "ultimatum",
        "step_id": step, "start_request_id": "browser-restart-after-recovery"})
    assert restarted.status_code == 200 and restarted.json()["run_id"] != started["run_id"]
    with main.conn() as connection:
        old = connection.execute("SELECT status,trial_count FROM instrument_runs WHERE run_id=?", (started["run_id"],)).fetchone()
    assert old["status"] == "superseded" and old["trial_count"] == 1


def test_completion_intent_survives_host_error_and_reload(browser_env):
    client, page, _, _ = browser_env
    identity, started, step = setup_module_run(client, "dd_lifetrade")
    records = [{"client_event_id": f"completion-event-{i}", "row": {"index": i}} for i in range(30)]
    for batch in (records[:25], records[25:]):
        assert client.post("/api/trials", json={"session_id": started["run_id"], "trials": batch}).status_code == 200
    assert page.evaluate("""async args=>{
      workflow.participantCode='TEST';workflow.invitationToken=args.token;workflow.currentStepId=args.step;
      workflow.workflowMode=true;workflow.testModuleOnly='dd_lifetrade';sessionId=args.run;participantCode='TEST';
      await BarsanStore.initialize();const normalApi=api;utDelay=async()=>{};
      api=async(path,...rest)=>{if(path==='/api/experiment/dd/complete')throw Object.assign(new Error('Synthetic host unavailable'),{status:503});return normalApi(path,...rest);};
      await finishDdTask();
      return (await BarsanStore.list()).filter(entry=>JSON.parse(entry.scope)[0]==='completion').length;
    }""", {"token": identity["invitation_token"], "run": started["run_id"], "step": step}) == 1
    page.reload()
    assert page.evaluate("""async args=>{
      workflow.participantCode='TEST';workflow.invitationToken=args.token;
      await recoverTaskCompletions();
      return (await BarsanStore.list()).filter(entry=>JSON.parse(entry.scope)[0]==='completion').length;
    }""", {"token": identity["invitation_token"]}) == 0
    replay = client.post("/api/experiment/stages/start", json={**identity, "stage_code": "dd_lifetrade", "step_id": step})
    assert replay.status_code == 200 and replay.json()["already_completed"] is True
