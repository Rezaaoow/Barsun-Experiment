"""Two isolated synthetic workflows through NEW Production HTTPS; no main-study writes."""
from pathlib import Path
from datetime import datetime,timezone
from urllib.parse import urlparse,parse_qs
import urllib.request,urllib.error,json,uuid,csv,io,time
BASE='https://experiment.barsun4818.ir/dce'
values={k:v for k,v in (line.split('=',1) for line in Path('/etc/barsan/application.env').read_text().splitlines() if '=' in line)}
token=values['ADMIN_TOKEN'];registry=[];runs=[];requests=0
def request(path,body=None,admin=False,raw=False):
 global requests
 headers={'X-Admin-Token':token} if admin else {}
 if body is not None:headers['Content-Type']='application/json'
 req=urllib.request.Request(BASE+path,data=json.dumps(body).encode() if body is not None else None,headers=headers)
 try:
  with urllib.request.urlopen(req,timeout=20) as response:
   requests+=1;data=response.read();assert response.status==200
 except urllib.error.HTTPError as error:raise AssertionError('HTTPS acceptance status '+str(error.code)) from None
 return data.decode() if raw else json.loads(data)
def stamp():return datetime.now(timezone.utc).isoformat()
def start(identity,step,stage):
 body={**identity,'step_id':step,'stage_code':stage,'start_request_id':str(uuid.uuid4())}
 result=request('/api/experiment/stages/start',body)
 assert request('/api/experiment/stages/start',body)['run_id']==result['run_id']
 return result
try:
 for variant in ['frame','control']:
  group=request('/api/admin/group-links',{'scope':'test','label':'Ubuntu Production acceptance '+variant,'steps':[{'step_id':'smoke-dd','module_code':'dd_standalone'},{'step_id':'smoke-rest','module_code':'intermission'},{'step_id':'smoke-ut','module_code':'ultimatum_'+variant}]},True)
  registry.append({'group_link_id':group['group_link_id'],'study_code':group['study_code']})
  assert group['study_code'].startswith('barsan-test-') and group['invitation_url'].split('?',1)[0].rstrip('/')==BASE
  group_token=parse_qs(urlparse(group['invitation_url']).query)['group'][0]
  claimed=request('/api/experiment/group/claim',{'group_token':group_token,'participant_code':'UBUNTU-SYNTHETIC-'+uuid.uuid4().hex[:12],'claim_request_id':str(uuid.uuid4())})
  identity={k:claimed[k] for k in ['invitation_token','participant_code']}
  request('/api/experiment/consent',{**identity,'accepted':True})
  identified=request('/api/experiment/identify',identity)
  assert identified['current_step_id']=='smoke-dd'
  dd=start(identity,'smoke-dd','dd_lifetrade');run=dd['run_id']
  rows=[{'client_event_id':str(uuid.uuid4()),'row':{'session_id':run,'task':'KT','magnitude':'100M','trial_in_block':n,'user_choice_type':'Now','reaction_time_ms':234,'response_method':'synthetic-production-acceptance'}} for n in range(1,6)]
  assert request('/api/trials',{'session_id':run,'trials':rows})['inserted_count']==5
  replay=request('/api/trials',{'session_id':run,'trials':rows});assert replay['inserted_count']==0 and replay['duplicate_count']==5
  request('/api/experiment/dd/complete',{**identity,'run_id':run,'stage_code':'dd_lifetrade','step_id':'smoke-dd'})
  rest=start(identity,'smoke-rest','intermission')
  rest_body={**identity,'run_id':rest['run_id'],'stage_code':'intermission','step_id':'smoke-rest','final_payload':{'continue_clicked_at':stamp()},'timing_checkpoints':[{'run_id':rest['run_id'],'clock_id':str(uuid.uuid4()),'step_id':'smoke-rest','stage_code':'intermission','sequence':1,'visible_ms':1000,'uncertain_ms':0,'closed':True}]}
  assert request('/api/experiment/stages/complete',rest_body)['completed']
  assert request('/api/experiment/stages/complete',rest_body)['completed']
  ut=start(identity,'smoke-ut','ultimatum');order=ut['draft_payload']['block_order'];assert len(order)==4
  specs=[{'phase':'practice','block_code':code,'block_order':0,'trial_in_block':1,'trial_sequence':0} for code in ['practice_proposer_agent','practice_responder_coworker']]
  specs += [{'phase':'main','block_code':code,'block_order':index+1,'trial_in_block':trial,'trial_sequence':index*5+trial} for index,code in enumerate(order) for trial in range(1,6)]
  total=0
  for spec in specs:
   round=request('/api/experiment/ultimatum/prepare',{**identity,'run_id':ut['run_id'],**spec,'client_metadata':{'source':'synthetic-production-acceptance'}})
   body={**identity,'run_id':ut['run_id'],'round_id':round['round_id'],'reaction_time_ms':234,'wait_duration_ms':2000,'responded_at':stamp(),'decision_screen_shown_at':stamp(),'client_metadata':{'source':'synthetic-production-acceptance'},'client_event_timestamps':{'waiting_started_at':stamp(),'waiting_ended_at':stamp()}}
   body.update({'participant_offer_to_opponent':4} if round['role']=='proposer' else {'participant_decision':'accept'})
   result=request('/api/experiment/ultimatum/resolve',body)
   repeated=request('/api/experiment/ultimatum/resolve',body)
   assert repeated['round_id']==result['round_id'] and repeated['points_earned']==result['points_earned']
   request('/api/experiment/ultimatum/events',{**identity,'run_id':ut['run_id'],'round_id':round['round_id'],'event_timestamps':{'result_screen_shown_at':stamp(),'advance_clicked_at':stamp()}})
   if spec['phase']=='main':total+=result['points_earned'];assert result['total_score']==total
  completion=request('/api/experiment/stages/complete',{**identity,'run_id':ut['run_id'],'stage_code':'ultimatum','step_id':'smoke-ut','final_payload':{'block_order':order,'final_score':total,'total_potential_points_lost':result['total_potential_lost'],'practice_excluded_from_score':True}})
  assert completion['completed']
  exported=list(csv.DictReader(io.StringIO(request('/api/admin/ultimatum/records.csv?scope=test',admin=True,raw=True))))
  selected=[r for r in exported if r['run_id']==ut['run_id']];assert len(selected)==22
  main=sorted([r for r in selected if r['phase']=='main'],key=lambda r:int(r['trial_sequence']))
  cumulative=0
  for row in main:
   cumulative+=int(row['points_earned']);assert int(row['final_score'])==cumulative and int(row['run_total_score'])==total
  runs.append({'variant':variant,'dd_responses':5,'ultimatum_rounds':22,'all_three_steps_completed':True,'idempotency_verified':True,'cumulative_export_verified':True})
 result={'passed':True,'https_api_requests':requests,'synthetic_workflows':runs,'main_study_participants_created':0,'old_host_changes':False,'capacity_test':False}
 print(json.dumps(result))
finally:
 if registry:
  request('/api/admin/group-links/status',{'scope':'test','group_link_ids':[r['group_link_id'] for r in registry],'active':False,'include_assignments':True},True)
  path=Path('/var/lib/barsan-bootstrap/2026-11-05-app/acceptance-registry.json');path.write_text(json.dumps(registry,indent=2));path.chmod(0o600)
