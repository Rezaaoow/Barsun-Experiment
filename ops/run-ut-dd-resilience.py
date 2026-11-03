"""Guarded on-host HTTPS API tests. Private invitation fixtures stay on this host."""
from pathlib import Path
from urllib.parse import urlparse, parse_qs
import hashlib, json, os, resource, signal, subprocess, time, uuid
import httpx
import psycopg
from psycopg.rows import dict_row
from psycopg.conninfo import conninfo_to_dict

os.umask(0o077)
ROOT=Path('/home/myexperi/lifetrade-staging')
JOB=ROOT/'deployments/ut-dd-resilience-20261103-68d22169'
WORK=JOB/'load-runs-v2'
BASE='https://staging.my-experiment.ir/lifetrade'
SHA='68d22169d8d599d4fe6bc2034873c6a58ab7f593'
K6=ROOT/'test-runs/capacity-fix-20261103/k6'
assert json.loads((ROOT/'current-release.json').read_text())['commit_sha']==SHA
assert K6.is_file() and not WORK.exists()
WORK.mkdir(mode=0o700)
values={}
for raw in (ROOT/'config/app.env').read_text().splitlines():
 line=raw.strip()
 if line and not line.startswith('#') and '=' in line:
  key,value=line.split('=',1);values[key.strip()]=value.strip()
assert conninfo_to_dict(values['DATABASE_URL'])['dbname']=='myexperi_barsan_staging'
assert values['STUDY_CODE'].startswith('barsan-staging-')
registry=[]
results=[]
client=httpx.Client(base_url=BASE,timeout=30,trust_env=False)

def health(production=False):
 start=time.monotonic()
 try:
  response=client.get(('https://my-experiment.ir/lifetrade' if production else BASE)+'/api/health',timeout=8)
  body=response.json()
  return {'ok':response.status_code==200 and body.get('database_connected') is True,'seconds':round(time.monotonic()-start,4),'at':time.time()}
 except Exception as error:return {'ok':False,'error_type':type(error).__name__,'at':time.time()}

def post(path,body,admin=False):
 response=client.post(path,json=body,headers={'X-Admin-Token':values['ADMIN_TOKEN']} if admin else {})
 assert response.status_code==200,'Staging fixture endpoint failed: '+path+' HTTP '+str(response.status_code)
 return response.json()

def cohort(label,count):
 participants=[];groups=[];studies=[]
 for variant in ('frame','control'):
  group=post('/api/admin/group-links',{'scope':'test','label':'UT-DD '+label+' '+variant,
   'steps':[{'step_id':'load-dd','module_code':'dd_lifetrade'},
            {'step_id':'load-ultimatum','module_code':'ultimatum_'+variant}]},True)
  assert group['study_code'].startswith('barsan-test-group-')
  groups.append(group['group_link_id']);studies.append(group['study_code'])
  registry.append({'group_link_id':group['group_link_id'],'study_code':group['study_code'],'label':label,'expected_count':count//2})
  (WORK/'cohort-registry.json').write_text(json.dumps(registry,indent=2))
  token=parse_qs(urlparse(group['invitation_url']).query)['group'][0]
  for index in range(count//2):
   code='UTDD68-'+label+'-'+variant+'-'+str(index+1)
   claimed=post('/api/experiment/group/claim',{'group_token':token,'participant_code':code,'claim_request_id':str(uuid.uuid4())})
   participants.append({'participant_code':claimed['participant_code'],'invitation_token':claimed['invitation_token'],'variant':variant})
  assert health(True)['ok'],'Production guard during fixture creation'
 path=WORK/(label+'-participants.json')
 path.write_text(json.dumps({'complete':True,'participants':participants},separators=(',',':')))
 os.chmod(path,0o600)
 print('COHORT_CREATED',label,count,flush=True)
 return path,studies

def verify(studies,count):
 with psycopg.connect(values['DATABASE_URL'],row_factory=dict_row) as connection:
  runs=connection.execute('SELECT ir.run_id,ir.status,ir.trial_count,iv.instrument_code FROM instrument_runs ir '
   'JOIN instrument_versions iv USING(instrument_version_id) JOIN study_participations sp USING(participation_id) '
   'WHERE sp.study_code=ANY(%s)',(studies,)).fetchall()
  dd=connection.execute('SELECT COUNT(*) AS n FROM lifetrade_trials t JOIN instrument_runs r USING(run_id) '
   'JOIN study_participations sp USING(participation_id) WHERE sp.study_code=ANY(%s)',(studies,)).fetchone()['n']
  ut=connection.execute("SELECT COUNT(*) AS n,COUNT(*) FILTER (WHERE t.status='completed') AS completed,"
   "COUNT(*) FILTER (WHERE t.wait_duration_ms IS NOT NULL) AS waited,"
   "COUNT(*) FILTER (WHERE t.payload->'client_event_timestamps'->>'advance_clicked_at' IS NOT NULL) AS advanced "
   'FROM ultimatum_trials t JOIN instrument_runs r USING(run_id) JOIN study_participations sp USING(participation_id) '
   'WHERE sp.study_code=ANY(%s)',(studies,)).fetchone()
  stages=connection.execute("SELECT COUNT(*) AS n,COUNT(*) FILTER (WHERE w.status='completed') AS completed "
   'FROM experiment_workflow_progress w JOIN experiment_assignments a USING(assignment_id) WHERE a.study_code=ANY(%s)',(studies,)).fetchone()
 correct=(len(runs)==count*2 and dd==count*30 and ut['n']==count*22 and ut['completed']==count*22
  and ut['waited']==count*22 and ut['advanced']==count*22 and stages['n']==count*2 and stages['completed']==count*2
  and all(row['status']=='completed' and row['trial_count']==(30 if row['instrument_code']=='lifetrade' else 22)
          and row['instrument_code'] in ('lifetrade','ultimatum') for row in runs))
 return {'correct':correct,'runs':len(runs),'dd_rows':dd,'ultimatum':dict(ut),'workflow_stages':dict(stages)}

def run(label,count,fixture,studies):
 assert all(health(True)['ok'] for _ in range(3))
 env=os.environ.copy();env.update(GOMAXPROCS='2',GOMEMLIMIT='1GiB',K6_NO_USAGE_REPORT='true')
 soft,hard=resource.getrlimit(resource.RLIMIT_NOFILE);resource.setrlimit(resource.RLIMIT_NOFILE,(min(4096,hard),hard))
 command=[str(K6),'run','--quiet','--summary-trend-stats','avg,min,med,max,p(90),p(95),p(99)',
  '--summary-export',str(WORK/(label+'-summary.json')),'-e','BASE_URL='+BASE,'-e','EXPECTED_USERS='+str(count),
  '-e','PARTICIPANTS_FILE='+str(fixture),str(JOB/'k6-ut-dd.js')]
 guard_samples=[];max_rss=0;failures=0;aborted=False;started=time.monotonic()
 with (WORK/(label+'.log')).open('w') as log:
  child=subprocess.Popen(command,stdout=log,stderr=subprocess.STDOUT,env=env)
  print('LOAD_STARTED',label,count,flush=True)
  try:
   while child.poll() is None:
    sample=health(True);guard_samples.append(sample);failures=0 if sample['ok'] else failures+1
    try:
     status=dict(line.split(':',1) for line in (Path('/proc')/str(child.pid)/'status').read_text().splitlines() if ':' in line)
     max_rss=max(max_rss,int(status.get('VmRSS','0 kB').split()[0]))
    except (OSError,ValueError):pass
    if failures>=2 or max_rss>1536*1024 or time.monotonic()-started>480:
     child.send_signal(signal.SIGINT);aborted=True;break
    time.sleep(5)
   code=child.wait(timeout=60)
  finally:
   if child.poll() is None:child.send_signal(signal.SIGINT);child.wait(timeout=60)
 (WORK/(label+'-production-guard.json')).write_text(json.dumps(guard_samples,indent=2))
 summary=json.loads((WORK/(label+'-summary.json')).read_text()) if (WORK/(label+'-summary.json')).exists() else {'metrics':{}}
 metrics=summary['metrics'];value=lambda name,key,default=0:metrics.get(name,{}).get('values',metrics.get(name,{})).get(key,metrics.get(name,{}).get('value',default) if key=='rate' else default)
 checked=verify(studies,count)
 result={'label':label,'users':count,'exit_code':code,'aborted':aborted,'duration_seconds':round(time.monotonic()-started,2),
  'completed':value('completed_participants','count'),'http_requests':value('http_reqs','count'),
  'http_failure_rate':value('http_req_failed','rate'),'p95_ms':value('http_req_duration','p(95)'),
  'p99_ms':value('http_req_duration','p(99)'),'prepare_requests':value('prepare_requests','count'),
  'terminal_failures':value('terminal_request_failures','count'),'retries':value('retry_attempts','count'),
  'app_p95_ms':value('server_app_ms','p(95)'),'pool_p95_ms':value('server_pool_ms','p(95)'),
  'sql_p95_ms':value('server_sql_ms','p(95)'),'outside_app_p95_ms':value('outside_app_wait_ms','p(95)'),
  'max_generator_rss_mb':round(max_rss/1024,2),'production_guard_probes':len(guard_samples),
  'production_guard_failures':sum(not row['ok'] for row in guard_samples),'database':checked}
 results.append(result);(WORK/'results.json').write_text(json.dumps(results,indent=2))
 print(json.dumps(result),flush=True)
 assert not aborted and checked['correct'] and result['completed']==count and result['terminal_failures']==0
 assert result['http_failure_rate']<.001 and result['p95_ms']<2000,'Stop before a larger cohort on unacceptable delay/failure'

failure=None
try:
 for label,count in [('smoke-2',2),('load-100',100),('load-300',300),('load-600',600)]:
  fixture,studies=cohort(label,count);run(label,count,fixture,studies)
except Exception as error:
 failure=type(error).__name__;print(json.dumps({'stopped_error_type':failure}),flush=True)
finally:
 if registry:
  group_ids=[row['group_link_id'] for row in registry];studies=[row['study_code'] for row in registry]
  assert all(study.startswith('barsan-test-group-') for study in studies)
  with psycopg.connect(values['DATABASE_URL']) as connection:
   count=connection.execute('UPDATE experiment_assignments SET active=0 WHERE group_link_id=ANY(%s) AND study_code=ANY(%s)',(group_ids,studies)).rowcount
   groups=connection.execute('UPDATE experiment_group_links SET active=0 WHERE group_link_id=ANY(%s) AND study_code=ANY(%s)',(group_ids,studies)).rowcount
  (WORK/'cleanup.json').write_text(json.dumps({'deactivated_assignments':count,'deactivated_groups':groups,'research_rows_deleted':0}))
  print('SYNTHETIC_LINKS_DEACTIVATED',count,groups,flush=True)
 final={'source_commit':SHA,'finished':True,'failure':failure,'results':results,
        'final_staging_health':health(),'final_production_health':health(True),'generator_shares_host_resources':True}
 (WORK/'final.json').write_text(json.dumps(final,indent=2));client.close()
raise SystemExit(1 if failure else 0)
