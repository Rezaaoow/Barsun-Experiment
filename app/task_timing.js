/* Visible-time checkpoints are independent of HTTP arrival time and never contain credentials. */
(()=>{
 'use strict';
 let clock=null,tick=null,writeChain=Promise.resolve(),flushPromise=null,retry=null;
 const prefix='barsan_clock_journal_v2_';
 const scope=row=>BarsanStore.scope('clock',row.assignment_id,row.run_id);
 const body=row=>{const copy={...row};delete copy.assignment_id;return copy;};
 function journal(row){localStorage.setItem(prefix+row.run_id,JSON.stringify(row));}
 function persist(row){
  const snapshot={...row};journal(snapshot);
  writeChain=writeChain.catch(()=>{}).then(async()=>{
   await BarsanStore.put(scope(snapshot),'latest',snapshot,'replace');
   const key=prefix+snapshot.run_id;
   if(localStorage.getItem(key)===JSON.stringify(snapshot))localStorage.removeItem(key);
  });
  return writeChain;
 }
 async function recover(assignment){
  for(const key of Object.keys(localStorage).filter(key=>key.startsWith(prefix))){
   let row;try{row=JSON.parse(localStorage.getItem(key));}catch(_){continue;}
   if(row?.assignment_id!==assignment||typeof row.run_id!=='string')continue;
   const saved=await BarsanStore.list(scope(row));
   if(!saved.length||saved[0].value.sequence<row.sequence)await persist(row);
   else localStorage.removeItem(key);
  }
 }
 function clearTick(){if(tick!==null)clearTimeout(tick);tick=null;}
 function sample(closed=false){
  if(!clock||clock.row.closed)return Promise.resolve();
  const perf=performance.now(),wall=Date.now();
  if(clock.visible){
   const delta=Math.max(0,perf-clock.perf);
   clock.total+=delta;
   if(delta>45000||wall-clock.wall>45000||Math.abs((wall-clock.wall)-delta)>2000)clock.uncertain+=delta;
  }
  clock.perf=perf;clock.wall=wall;
  clock.row={...clock.row,sequence:clock.row.sequence+1,visible_ms:Math.round(clock.total),
   uncertain_ms:Math.min(Math.round(clock.total),Math.round(clock.uncertain)),closed};
  return persist(clock.row);
 }
 function schedule(){
  if(tick!==null||!clock||clock.row.closed||!clock.visible)return;
  tick=setTimeout(()=>{
   tick=null;
   // Schedule sampling before waiting for either storage or the network.
   const saved=sample();schedule();
   saved.then(()=>flush()).catch(()=>{});
  },27000+Math.floor(Math.random()*6000));
 }
 async function start(assignment,run,step,stage){
  if(!assignment||!run||!['dd_lifetrade','ultimatum'].includes(stage))return false;
  if(clock?.row.run_id===run){
   if(!clock.row.closed&&!clock.visible){clock.visible=!document.hidden;clock.perf=performance.now();clock.wall=Date.now();schedule();}
   return !clock.row.closed;
  }
  if(clock&&!clock.row.closed)await close();
  await recover(assignment);
  const existing=await BarsanStore.list(BarsanStore.scope('clock',assignment,run));
  const row=existing[0]?.value||{assignment_id:assignment,run_id:run,clock_id:crypto.randomUUID(),step_id:step,
   stage_code:stage,sequence:0,visible_ms:0,uncertain_ms:0,closed:false};
  clock={row,total:row.visible_ms,uncertain:row.uncertain_ms,visible:!document.hidden,perf:performance.now(),wall:Date.now()};
  if(!row.closed){await sample();schedule();}
  flush().catch(()=>{});return !row.closed;
 }
 async function pause(){if(!clock||clock.row.closed)return;clearTick();const saved=sample();clock.visible=false;await saved;flush().catch(()=>{});}
 async function close(){if(!clock||clock.row.closed)return;clearTick();const saved=sample(true);clock.visible=false;await saved;}
 async function pending(assignment){
  await recover(assignment);await writeChain;
  return (await BarsanStore.list()).filter(e=>JSON.parse(e.scope)[0]==='clock'&&e.value.assignment_id===assignment).slice(0,25);
 }
 async function acknowledge(entries,result){
  const saved=new Set((result.acknowledged_checkpoints||[]).map(r=>r.run_id+':'+r.sequence));
  await BarsanStore.ack(entries.filter(e=>saved.has(e.value.run_id+':'+e.value.sequence)));
 }
 async function flush(){
  if(flushPromise||!workflow.assignmentId||!workflow.invitationToken)return flushPromise;
  flushPromise=(async()=>{
   const entries=await pending(workflow.assignmentId);if(!entries.length)return;
   const result=await apiWithRetry('/api/experiment/time/checkpoints',{
    invitation_token:workflow.invitationToken,participant_code:workflow.participantCode,checkpoints:entries.map(e=>body(e.value))
   },{attempts:2,timeoutMs:10000,priority:'telemetry'});
   await acknowledge(entries,result);
  })();
  try{return await flushPromise;}
  finally{
   flushPromise=null;
   if(retry===null)retry=setTimeout(()=>{retry=null;flush().catch(()=>{});},15000+Math.random()*15000);
  }
 }
 window.addEventListener('online',()=>flush().catch(()=>{}));
 window.addEventListener('pagehide',()=>{if(clock&&!clock.row.closed){clearTick();sample().catch(()=>{});clock.visible=false;}});
 globalThis.BarsanClock=Object.freeze({start,pause,close,pending,acknowledge,body,flush,
  currentRun:()=>clock?.row.run_id,snapshot:()=>clock?{...clock.row}:null});
})();
