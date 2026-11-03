import http from 'k6/http';
import {check,sleep} from 'k6';
import {Counter,Trend} from 'k6/metrics';
import {SharedArray} from 'k6/data';

const base='https://staging.my-experiment.ir/lifetrade';
if(__ENV.BASE_URL!==base)throw new Error('Dedicated Staging HTTPS only');
const users=Number(__ENV.EXPECTED_USERS);
if(![2,100,300,600].includes(users))throw new Error('Unexpected cohort size');
const participants=new SharedArray('ut-dd-private-fixture',()=>{
 const fixture=JSON.parse(open(__ENV.PARTICIPANTS_FILE));
 if(!fixture.complete||fixture.participants.length!==users)throw new Error('Incomplete private fixture');
 return fixture.participants;
});
const completed=new Counter('completed_participants');
const prepares=new Counter('prepare_requests');
const terminal=new Counter('terminal_request_failures');
const retries=new Counter('retry_attempts');
const appMs=new Trend('server_app_ms');
const poolMs=new Trend('server_pool_ms');
const sqlMs=new Trend('server_sql_ms');
const outsideMs=new Trend('outside_app_wait_ms');
export const options={
 scenarios:{tasks:{executor:'per-vu-iterations',vus:users,iterations:1,maxDuration:'36m',exec:'participantFlow'}},
 thresholds:{http_req_failed:['rate<0.001'],http_req_duration:['p(95)<500','p(99)<1500'],
  checks:['rate>0.999'],completed_participants:[`count==${users}`],prepare_requests:[`count==${users}`],terminal_request_failures:['count==0']}
};
const id=prefix=>prefix+'-'+__VU+'-'+Date.now()+'-'+Math.random().toString(16).slice(2);
const stamp=()=>new Date().toISOString();
function observe(response,path){
 const header=Object.entries(response.headers||{}).find(([name])=>name.toLowerCase()==='server-timing');
 if(header){
  const values={};for(const match of String(header[1]).matchAll(/(app|pool|sql);dur=([0-9.]+)/g))values[match[1]]=Number(match[2]);
  if(values.app!==undefined){appMs.add(values.app,{api_path:path});poolMs.add(values.pool||0,{api_path:path});sqlMs.add(values.sql||0,{api_path:path});outsideMs.add(Math.max(0,response.timings.waiting-values.app),{api_path:path});}
 }
}
async function post(path,payload,auxiliary=false){
 for(let attempt=0;attempt<3;attempt++){
  const response=await http.asyncRequest('POST',base+path,JSON.stringify(payload),{
   headers:{'Content-Type':'application/json','Accept':'application/json'},timeout:'15s',tags:{api_path:path}
  });
  observe(response,path);
  if(response.status>=200&&response.status<300){check(response,{'HTTP final response 2xx':()=>true});return response.json();}
  const retryable=[0,408,425,429,500,502,503,504].includes(response.status);
  if(retryable&&attempt<2){
   retries.add(1);let pause=Math.random()*Math.min(8000,500*Math.pow(2,attempt));
   const retryAfter=response.headers['Retry-After'];
   if(retryAfter){const seconds=Number(retryAfter),date=Date.parse(retryAfter);pause=Math.max(pause,Number.isFinite(seconds)?Math.min(60000,seconds*1000):Number.isFinite(date)?Math.max(0,Math.min(60000,date-Date.now())):0);}
   sleep(pause/1000);continue;
  }
  terminal.add(1,{api_path:path});check(response,{'HTTP final response 2xx':()=>false});
  if(auxiliary)return null;
  throw new Error(path+' failed with HTTP '+response.status);
 }
}
export function setup(){
 const response=http.get(base+'/api/health',{timeout:'10s'});
 const body=response.json();
 if(response.status!==200||!body.database_connected||body.database!=='postgresql'||!body.study_code.startsWith('barsan-staging-'))throw new Error('Staging health guard failed');
 terminal.add(0);prepares.add(0);completed.add(0);retries.add(0);
}
function timeTracker(identity,stage,step,run){
 let last=0,sequence=0;const started=Date.now(),clock=id('clock');
 return async(event='heartbeat')=>{
  if(event==='heartbeat'&&Date.now()-last<27000)return;
  last=Date.now();sequence++;
  await post('/api/experiment/time/checkpoints',{...identity,checkpoints:[{
   run_id:run,clock_id:clock,step_id:step,stage_code:stage,sequence,
   visible_ms:Date.now()-started,uncertain_ms:0,closed:event==='complete'
  }]});
 };
}
export async function participantFlow(){
 // Spread entry without changing the experiment's own prescribed delays.
 sleep(users===2?0:Math.random()*30);
 const person=participants[__VU-1];
 const identity={participant_code:person.participant_code,invitation_token:person.invitation_token};
 await post('/api/experiment/consent',{...identity,accepted:true});
 const identified=await post('/api/experiment/identify',identity);
 if(identified.current_step_id!=='load-dd')throw new Error('Unexpected workflow');
 const dd=await post('/api/experiment/stages/start',{...identity,stage_code:'dd_lifetrade',step_id:'load-dd',start_request_id:id('dd-start')});
 const ddTime=timeTracker(identity,'dd_lifetrade','load-dd',dd.run_id);await ddTime('start');
 for(let offset=0;offset<30;offset+=5){
  // A synthetic batch every 7.5s stresses writes more often than a full browser's
  // fixation/decision/intertrial schedule. This is an API load, not a timing study.
  sleep(users===2 ? 0.1 : 7.5);
  const trials=[];
  for(let index=offset;index<offset+5;index++)trials.push({client_event_id:dd.run_id+'-dd-'+index,row:{
   task:index<15?'LT':'KT',task_order:index<15?1:2,block_name:['Low','Mid','High'][Math.floor(index%15/5)],
   magnitude:'1M',trial_in_block:index%5+1,index:index+1,user_choice_type:index%2?'Later':'Now',
   reaction_time_ms:500,response_method:'synthetic',trial_started_iso:stamp(),response_iso:stamp(),visibility_hidden:false
  }});
  await post('/api/trials',{session_id:dd.run_id,trials});await ddTime();
 }
 await post('/api/experiment/dd/complete',{...identity,stage_code:'dd_lifetrade',step_id:'load-dd',run_id:dd.run_id});
 await ddTime('complete');
 const ut=await post('/api/experiment/stages/start',{...identity,stage_code:'ultimatum',step_id:'load-ultimatum',start_request_id:id('ut-start')});
 const utTime=timeTracker(identity,'ultimatum','load-ultimatum',ut.run_id);await utTime('start');
 if(users===600){
  // A long instruction/idle phase checks sustained clocks and connection recovery.
  for(let part=0;part<37;part++){
   sleep(30);await utTime();
   if(part===18){const again=await post('/api/experiment/identify',identity);if(again.current_step_id!=='load-ultimatum')throw new Error('Idle re-entry changed workflow');}
  }
 }

 const order=ut.draft_payload.block_order;
 if(!Array.isArray(order)||new Set(order).size!==4)throw new Error('Invalid server block order');
 const specs=[{phase:'practice',block_code:'practice_proposer_agent',block_order:0,trial_in_block:1,trial_sequence:0},
  {phase:'practice',block_code:'practice_responder_coworker',block_order:0,trial_in_block:1,trial_sequence:0}];
 order.forEach((code,index)=>{for(let trial=1;trial<=5;trial++)specs.push({phase:'main',block_code:code,block_order:index+1,trial_in_block:trial,trial_sequence:index*5+trial});});
 let cache=null,events=[],lastResult=null;const summaries={};
 for(const spec of specs){
  let round;
  if(cache&&Object.keys(spec).every(key=>cache.request[key]===spec[key]))round=cache.round;
  else{prepares.add(1);round=await post('/api/experiment/ultimatum/prepare',{...identity,run_id:ut.run_id,...spec,client_metadata:{source:'staging-k6-ut-dd'}});}
  cache=null;
  const proposer=round.role==='proposer',waitSeconds=users===2 ? 0.01 : spec.block_code.includes('agent') ? 2 : 3+Math.random()*3;
  const waitingStarted=Date.now(),waitingAt=stamp();
  const payload={...identity,run_id:ut.run_id,round_id:round.round_id,prepare_next:true,
   reaction_time_ms:500,responded_at:stamp(),decision_screen_shown_at:stamp(),
   client_metadata:{source:'staging-k6-ut-dd'},auxiliary_events:events.slice(),
   planned_wait_duration_ms:Math.round(waitSeconds*1000),wait_duration_ms:null,
   client_event_timestamps:{waiting_started_at:waitingAt}};
  let request;
  if(proposer){payload.participant_offer_to_opponent=4;request=post('/api/experiment/ultimatum/resolve',payload);sleep(waitSeconds);}
  else{
   sleep(waitSeconds);payload.participant_decision='accept';payload.wait_duration_ms=Date.now()-waitingStarted;
   payload.client_event_timestamps.waiting_ended_at=stamp();request=post('/api/experiment/ultimatum/resolve',payload);
  }
  const waited=Date.now()-waitingStarted,waitingEnded=stamp();
  const result=await request;
  const ack=new Set(result.acknowledged_event_round_ids||[]);events=events.filter(row=>!ack.has(row.round_id));
  if(result.next_round)cache={round:result.next_round,request:result.next_round_request};
  const timestamps={result_screen_shown_at:stamp(),advance_clicked_at:stamp()};
  if(proposer)timestamps.waiting_ended_at=waitingEnded;
  events.push({round_id:round.round_id,event_timestamps:timestamps,...(proposer?{wait_duration_ms:waited}:{})});
  if(spec.phase==='main'){
   lastResult=result;
   summaries[spec.block_code]={block_order:spec.block_order,points_earned:result.block_score,potential_points_lost:result.block_potential_lost};
  }
  await utTime();
  if(users===600)sleep(25);
 }
 const finished=await post('/api/experiment/stages/complete',{
  ...identity,stage_code:'ultimatum',step_id:'load-ultimatum',run_id:ut.run_id,auxiliary_events:events,
  final_payload:{schema_version:1,condition_code:identified.condition_code,feedback_group:person.variant==='control'?'Control':'Frame',
   block_order:order,final_score:lastResult.total_score,total_potential_points_lost:lastResult.total_potential_lost,
   block_summaries:summaries,practice_excluded_from_score:true,client_completed_at:stamp()}
 });
 if(!finished.completed||(finished.acknowledged_event_round_ids||[]).length!==events.length)throw new Error('Completion or final timing ack missing');
 await utTime('complete');completed.add(1);
}
