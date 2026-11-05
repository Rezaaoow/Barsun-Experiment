// NEW Production only. Private fixture contains NEW synthetic group links only.
import http from 'k6/http';
import {check,sleep} from 'k6';
import {Counter,Trend} from 'k6/metrics';
const base='https://experiment.barsun4818.ir/dce';
if(__ENV.BASE_URL!==base)throw Error('NEW Production URL required');
const fixture=JSON.parse(open(__ENV.FIXTURE_FILE));
if(!fixture.synthetic||!fixture.groups.every(g=>g.study_code.startsWith('barsan-test-')))throw Error('Synthetic scope required');
const users=Number(__ENV.USERS||24),offset=Number(__ENV.OFFSET||0),burst=__ENV.MODE==='burst',fast=__ENV.FAST==='1';
if(![2,24,100,300,500,600].includes(users)||offset<0)throw Error('Invalid cohort');
const completed=new Counter('completed_participants'),terminal=new Counter('terminal_failures'),retry=new Counter('retry_attempts'),replays=new Counter('lost_ack_replays');
const utAllocationMs=new Trend('ultimatum_allocation_server_ms');
const utStartMs=new Trend('ultimatum_start_ms'),ddStartMs=new Trend('dd_start_ms');
const allocation=new Trend('allocation_request_ms'),claimMs=new Trend('claim_request_ms'),logicalMs=new Trend('logical_request_ms'),appMs=new Trend('app_ms'),poolMs=new Trend('pool_ms'),allocationMs=new Trend('allocation_server_ms');
export const options={scenarios:{journeys:{executor:'per-vu-iterations',vus:users,iterations:1,maxDuration:'12m',exec:'journey'}},
 systemTags:['status','method','name','scenario','error_code','check','group'],summaryTrendStats:['avg','med','p(95)','p(99)','max'],
 thresholds:{checks:['rate==1'],completed_participants:[`count==${users}`],terminal_failures:['count==0'],http_req_failed:['rate<0.001'],http_req_duration:['p(95)<1000','p(99)<3000']}};
const uuid=()=> 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g,c=>{const r=Math.random()*16|0;return(c==='x'?r:(r&3|8)).toString(16)});
const stamp=()=>new Date().toISOString();
const planKey=p=>JSON.stringify(Object.keys(p).sort().map(k=>[k,p[k]]));
function observe(r,path){
 const h=Object.entries(r.headers||{}).find(([k])=>k.toLowerCase()==='server-timing');const m={};
 if(h)for(const item of String(h[1]).matchAll(/(app|pool|allocation);dur=([0-9.]+)/g))m[item[1]]=Number(item[2]);
 if(m.app!==undefined){appMs.add(m.app);poolMs.add(m.pool||0);allocationMs.add(m.allocation||0);}
 if(path==='/api/experiment/group/claim')claimMs.add(r.timings.duration);
 if(path==='/api/experiment/stages/start')allocation.add(r.timings.duration);
}
function post(path,payload,replay=false){
 const begin=Date.now();
 // Independent synthetic session profiles. Application-layer RTT/jitter simulation,
 // not 600 distinct networks or a network-shaping/TCP test.
 if(!fast&&!burst)sleep((40+(__VU%5)*80+Math.random()*100)/1000);
 for(let attempt=0;attempt<6;attempt++){
  const r=http.post(base+path,JSON.stringify(payload),{headers:{'Content-Type':'application/json'},timeout:'15s',tags:{name:path}});observe(r,path);
  if(r.status>=200&&r.status<300){
   if(path==='/api/experiment/stages/start'){if(payload.stage_code==='ultimatum'){utStartMs.add(r.timings.duration);const h=Object.entries(r.headers).find(([k])=>k.toLowerCase()==='server-timing');const m=h&&String(h[1]).match(/allocation;dur=([0-9.]+)/);if(m)utAllocationMs.add(Number(m[1]));}if(payload.stage_code==='dd_lifetrade')ddStartMs.add(r.timings.duration);}
   if(replay){replays.add(1);replay=false;continue;}
   logicalMs.add(Date.now()-begin);check(r,{'logical operation succeeds':()=>true});return r.json();
  }
  if([0,408,425,429,500,502,503,504].includes(r.status)&&attempt<5){retry.add(1);sleep(Math.max(Number(r.headers['Retry-After']||0),Math.random()*Math.min(8,0.5*2**attempt)));continue;}
  terminal.add(1);check(r,{'logical operation succeeds':()=>false});throw Error('Synthetic '+path+' status '+r.status);
 }
 throw Error('Synthetic retry budget exhausted');
}
export function setup(){
 const r=http.get(base+'/api/health',{tags:{name:'health'},timeout:'10s'});const body=r.json();
 if(r.status!==200||body.database!=='postgresql'||!body.database_connected)throw Error('Health guard');
 completed.add(0);terminal.add(0);retry.add(0);replays.add(0);return {allocation_at:Date.now()+25000};
}
export function journey(data){
 const index=offset+__VU,g=fixture.groups[(index-1)%2],code=fixture.prefix+'-'+String(index).padStart(4,'0');
 const shouldReplay=index%20===0;
 if(!burst&&!fast)sleep(Math.random()*Number(__ENV.ENTRY_SPREAD_SECONDS||60));
 const entry=http.get(base+'/?group='+g.group_token,{timeout:'15s',tags:{name:'participant_entry'}});
 check(entry,{'active synthetic link entry loads':r=>r.status===200&&r.body.includes('participant-first-name')});
 const font=http.get(base+'/assets/vazirmatn-v33.003.woff2',{responseType:'none',timeout:'15s',tags:{name:'local_font'}});
 check(font,{'local font loads':r=>r.status===200});
 const claim=post('/api/experiment/group/claim',{group_token:g.group_token,participant_code:code,first_name:'Synthetic',last_name:'Acceptance',claim_request_id:uuid()},shouldReplay);
 const identity={participant_code:claim.participant_code,invitation_token:claim.invitation_token};
 post('/api/experiment/consent',{...identity,first_name:'Synthetic',last_name:'Acceptance',accepted:true});
 const identified=post('/api/experiment/identify',identity);
 const time=(stage,step,event)=>post('/api/experiment/time',{...identity,stage_code:stage,step_id:step,event,event_id:uuid()});
 const clocks={};
 function clock(run,stage,step,closed=false){
  if(!clocks[run])clocks[run]={started:Date.now(),sequence:0,clock_id:uuid()};
  const c=clocks[run];c.sequence++;
  const row={run_id:run,stage_code:stage,step_id:step,clock_id:c.clock_id,sequence:c.sequence,visible_ms:Date.now()-c.started,uncertain_ms:0,closed};
  const result=post('/api/experiment/time/checkpoints',{...identity,checkpoints:[row]});
  check(result,{'visible-time checkpoint acknowledged':r=>r.acknowledged_checkpoints?.[0]?.sequence===row.sequence});
 }

 if(!burst){
  const ddBody={...identity,stage_code:'dd_lifetrade',step_id:'load-dd',start_request_id:uuid()};
  const dd=post('/api/experiment/stages/start',ddBody,shouldReplay);time('dd_lifetrade','load-dd','start');clock(dd.run_id,'dd_lifetrade','load-dd');
  const position=dd.draft_payload.dd_positions.now_left;
  check(position,{'DD plan balanced and stable':p=>p.length===5&&[2,3].includes(p.filter(Boolean).length)});
  const rows=[];let current=15,stepSize=8;
  for(let i=1;i<=5;i++){
   if(!fast)sleep(1.5+Math.random()*2);
   const choice=(index+i)%2?'Later':'Now',condition=fixture.dd_conditions[current],next=Math.max(0,Math.min(30,current+(choice==='Now'?-stepSize:stepSize)));
   const left=position[i-1]?'Now':'Later';
   rows.push({client_event_id:dd.run_id+'-dd-'+i,row:{task:'KT',task_order:1,block_name:'Standalone',magnitude:'100M',trial_in_block:i,index:i,user_choice_type:choice,
    delay:condition.delay,choice:condition.choice,index:condition.index,requested_row:current,displayed_row:current+1,requested_next_row:next,bounded_next_row:next,boundary_hit:false,left_stimulus:left,right_stimulus:left==='Now'?'Later':'Now',now_on_left:position[i-1],reaction_time_ms:750,response_method:'synthetic',trial_started_iso:stamp(),response_iso:stamp(),visibility_hidden:false}});
   current=next;stepSize=Math.max(1,Math.floor(stepSize/2));
  }
  post('/api/trials',{session_id:dd.run_id,trials:rows},shouldReplay);
  post('/api/experiment/dd/complete',{...identity,stage_code:'dd_lifetrade',step_id:'load-dd',run_id:dd.run_id});time('dd_lifetrade','load-dd','complete');clock(dd.run_id,'dd_lifetrade','load-dd',true);
  const rest=post('/api/experiment/stages/start',{...identity,stage_code:'intermission',step_id:'load-rest',start_request_id:uuid()});
  time('intermission','load-rest','start');clock(rest.run_id,'intermission','load-rest');if(!fast)sleep(1+Math.random()*3);
  post('/api/experiment/stages/complete',{...identity,stage_code:'intermission',step_id:'load-rest',run_id:rest.run_id,final_payload:{schema_version:1,continue_clicked_at:stamp(),source:'synthetic-acceptance'}});time('intermission','load-rest','complete');clock(rest.run_id,'intermission','load-rest',true);
 }
 if(burst)sleep(Math.max(0,(data.allocation_at-Date.now())/1000));
 const startBody={...identity,stage_code:'ultimatum',step_id:'load-ut',start_request_id:uuid()};
 let ut=post('/api/experiment/stages/start',startBody,shouldReplay),plan=ut.draft_payload.randomization;
 check(plan,{'full-24 saved server allocation':p=>p.algorithm==='full24-williams-quartets-v1'&&p.stratum===g.variant});
 // Refresh creates a fresh attempt, but preserves the original allocation.
 if(index%50===0){const restarted=post('/api/experiment/stages/start',{...startBody,start_request_id:uuid()});check(restarted,{'restart keeps allocation':r=>planKey(r.draft_payload.randomization)===planKey(plan)&&r.run_id!==ut.run_id});ut=restarted;}
 if(burst){completed.add(1);return;}
 time('ultimatum','load-ut','start');clock(ut.run_id,'ultimatum','load-ut');
 if(!fast)sleep(3+Math.random()*3);
 const order=plan.block_order,specs=[{phase:'practice',block_code:'practice_proposer_agent',block_order:0,trial_in_block:1,trial_sequence:0},{phase:'practice',block_code:'practice_responder_coworker',block_order:0,trial_in_block:1,trial_sequence:0}];
 order.forEach((code,i)=>{for(let t=1;t<=5;t++)specs.push({phase:'main',block_code:code,block_order:i+1,trial_in_block:t,trial_sequence:i*5+t});});
 let cache=null,events=[],last=null;const summaries={};
 for(let n=0;n<specs.length;n++){
  const spec=specs[n];let round;
  if(cache&&Object.keys(spec).every(k=>cache.request[k]===spec[k]))round=cache.round;
  else round=post('/api/experiment/ultimatum/prepare',{...identity,run_id:ut.run_id,...spec,client_metadata:{source:'synthetic-production-final'}});
  cache=null;
  const proposer=round.role==='proposer',wait=fast?0.01:spec.block_code.includes('agent')?2:3+Math.random()*3;
  if(!fast)sleep(0.5+Math.random()*1.5);
  const waitStart=Date.now(),timestamps={waiting_started_at:stamp()};
  const payload={...identity,run_id:ut.run_id,round_id:round.round_id,prepare_next:true,reaction_time_ms:750,responded_at:stamp(),decision_screen_shown_at:stamp(),
   client_metadata:{source:'synthetic-production-final'},auxiliary_events:events.slice(),planned_wait_duration_ms:Math.round(wait*1000),client_event_timestamps:timestamps};
  // API generator keeps prescribed waits, without claiming browser timing validity.
  sleep(wait);timestamps.waiting_ended_at=stamp();payload.wait_duration_ms=Date.now()-waitStart;
  if(proposer)payload.participant_offer_to_opponent=4;else payload.participant_decision=(index+n)%3?'accept':'reject';
  if(shouldReplay&&n===7&&!fast)sleep(2+Math.random()*3); // queued brief disconnection profile
  const result=post('/api/experiment/ultimatum/resolve',payload,shouldReplay&&n===7);
  const ack=new Set(result.acknowledged_event_round_ids||[]);events=events.filter(e=>!ack.has(e.round_id));
  if(result.next_round)cache={round:result.next_round,request:result.next_round_request};
  events.push({round_id:round.round_id,event_timestamps:{result_screen_shown_at:stamp(),advance_clicked_at:stamp()}});
  if(spec.phase==='main'){last=result;summaries[spec.block_code]={block_order:spec.block_order,points_earned:result.block_score,potential_points_lost:result.block_potential_lost};}
  if(!fast&&n%5===0){time('ultimatum','load-ut','heartbeat');clock(ut.run_id,'ultimatum','load-ut');}
 }
 const done=post('/api/experiment/stages/complete',{...identity,stage_code:'ultimatum',step_id:'load-ut',run_id:ut.run_id,auxiliary_events:events,
  final_payload:{schema_version:1,condition_code:identified.condition_code,feedback_group:g.variant,block_order:order,final_score:last.total_score,total_potential_points_lost:last.total_potential_lost,block_summaries:summaries,practice_excluded_from_score:true,client_completed_at:stamp()}});
 check(done,{'final timing acknowledged':d=>d.completed&&(d.acknowledged_event_round_ids||[]).length===events.length});
 time('ultimatum','load-ut','complete');clock(ut.run_id,'ultimatum','load-ut',true);completed.add(1);
}
