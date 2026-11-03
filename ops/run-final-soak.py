"""Staging-only prolonged UT/DD API test with host-private synthetic fixtures."""
from pathlib import Path
from urllib.parse import parse_qs,urlparse
import json,os,resource,signal,subprocess,threading,time,uuid
import httpx,psycopg
from psycopg.rows import dict_row
from psycopg.conninfo import conninfo_to_dict
os.umask(0o077)
ROOT=Path('/home/myexperi/lifetrade-staging')
SHA='85c2158bdb9bc709a6696d489993c532c5703cde'
JOB=ROOT/'deployments'/('final-'+SHA[:12]);WORK=JOB/'soak-30m'
BASE='https://staging.my-experiment.ir/lifetrade'
K6=ROOT/'test-runs/capacity-fix-20261103/k6'
assert json.loads((ROOT/'current-release.json').read_text())['commit_sha']==SHA
assert not WORK.exists();WORK.mkdir(mode=0o700)
values={}
for raw in (ROOT/'config/app.env').read_text().splitlines():
 if raw.strip() and not raw.startswith('#') and '=' in raw:
  key,value=raw.split('=',1);values[key.strip()]=value.strip()
assert conninfo_to_dict(values['DATABASE_URL'])['dbname']=='myexperi_barsan_staging'
registry=[];results=[];health_samples=[];export_samples=[];done=threading.Event()
client=httpx.Client(base_url=BASE,timeout=30,trust_env=False)
def post(path,body,admin=False):
 r=client.post(path,json=body,headers={'X-Admin-Token':values['ADMIN_TOKEN']} if admin else {})
 assert r.status_code==200,'Synthetic Staging setup failed';return r.json()
def health_worker():
 with httpx.Client(timeout=10,trust_env=False) as c:
  while not done.is_set():
   t=time.monotonic()
   try:
    r=c.get(BASE+'/api/health');ok=r.status_code==200 and r.json().get('database_connected') is True
   except Exception:ok=False
   health_samples.append({'ok':ok,'ms':round((time.monotonic()-t)*1000,2),'at':time.time()})
   (WORK/'health-summary-private.json').write_text(json.dumps(health_samples))
   done.wait(10)
def exports_worker():
 with httpx.Client(timeout=httpx.Timeout(15,read=45),trust_env=False) as c:
  iteration=0
  while not done.wait(60):
   iteration+=1;t=time.monotonic();size=0
   path=('/api/admin/dd/records.csv?scope=test' if iteration==22 else
         '/api/admin/ultimatum/records.csv?scope=test' if iteration==24 else
         '/api/admin/time/checkpoints.csv?scope=test' if iteration%3==0 else
         '/api/admin/progress?scope=test&include_timing=true')
   try:
    with c.stream('GET',BASE+path,headers={'X-Admin-Token':values['ADMIN_TOKEN']}) as r:
     ok=r.status_code==200
     for chunk in r.iter_bytes(chunk_size=65536):size+=len(chunk)
   except Exception:ok=False
   export_samples.append({'ok':ok,'kind':'csv' if '.csv' in path else 'progress','bytes':size,'seconds':round(time.monotonic()-t,3)})
   (WORK/'export-summary-private.json').write_text(json.dumps(export_samples))
def cohort(label,count):
 people=[];studies=[];group_ids=[]
 for variant in ['frame','control']:
  g=post('/api/admin/group-links',{'scope':'test','label':'Final soak '+label+' '+variant,'steps':[{'step_id':'load-dd','module_code':'dd_lifetrade'},{'step_id':'load-ultimatum','module_code':'ultimatum_'+variant}]},True)
  assert g['study_code'].startswith('barsan-test-group-')
  token=parse_qs(urlparse(g['invitation_url']).query)['group'][0]
  registry.append({'study_code':g['study_code'],'group_link_id':g['group_link_id']})
  (WORK/'registry-private.json').write_text(json.dumps(registry))
  studies.append(g['study_code']);group_ids.append(g['group_link_id'])
  for i in range(count//2):
   p=post('/api/experiment/group/claim',{'group_token':token,'participant_code':'FINAL85-'+label+'-'+variant+'-'+str(i+1),'claim_request_id':str(uuid.uuid4())})
   people.append({'participant_code':p['participant_code'],'invitation_token':p['invitation_token'],'variant':variant})
 fixture=WORK/(label+'-private.json');fixture.write_text(json.dumps({'complete':True,'participants':people}));os.chmod(fixture,0o600)
 return fixture,studies
def verify(studies,count):
 with psycopg.connect(values['DATABASE_URL'],row_factory=dict_row) as c:
  common=' JOIN instrument_runs r USING(run_id) JOIN study_participations sp USING(participation_id) WHERE sp.study_code=ANY(%s)'
  dd=c.execute('SELECT COUNT(*) AS n FROM lifetrade_trials t'+common,(studies,)).fetchone()['n']
  ut=c.execute('SELECT COUNT(*) AS n FROM ultimatum_trials t'+common,(studies,)).fetchone()['n']
  completed=c.execute("SELECT COUNT(*) AS n FROM instrument_runs r JOIN study_participations sp USING(participation_id) WHERE sp.study_code=ANY(%s) AND r.status='completed'",(studies,)).fetchone()['n']
  clocks=c.execute('SELECT COUNT(*) AS n,SUM(t.closed) AS closed,SUM(t.uncertain_ms) AS uncertain_ms FROM experiment_client_clocks t'+common,(studies,)).fetchone()
  events=c.execute('SELECT COUNT(*) AS n FROM experiment_client_clock_checkpoints t'+common,(studies,)).fetchone()['n']
  timing=c.execute('SELECT COUNT(*) AS n FROM ultimatum_trials t'+common+" AND t.wait_duration_ms IS NOT NULL AND t.payload->'client_event_timestamps'->>'advance_clicked_at' IS NOT NULL",(studies,)).fetchone()['n']
 return {'dd_rows':dd,'ut_rows':ut,'completed_runs':completed,'clocks':clocks['n'],'closed_clocks':clocks['closed'],'checkpoints':events,'ut_complete_timing_rows':timing,
         'correct':dd==count*30 and ut==count*22 and completed==count*2 and clocks['n']==count*2 and clocks['closed']==count*2 and timing==count*22}
def run(label,count):
 fixture,studies=cohort(label,count)
 env=os.environ.copy();env.update(GOMAXPROCS='2',GOMEMLIMIT='1GiB',K6_NO_USAGE_REPORT='true')
 soft,hard=resource.getrlimit(resource.RLIMIT_NOFILE);resource.setrlimit(resource.RLIMIT_NOFILE,(min(4096,hard),hard))
 command=[str(K6),'run','--quiet','--summary-trend-stats','avg,min,med,max,p(95),p(99)','--summary-export',str(WORK/(label+'-summary.json')),
          '-e','BASE_URL='+BASE,'-e','EXPECTED_USERS='+str(count),'-e','PARTICIPANTS_FILE='+str(fixture),str(JOB/'k6-final-soak.js')]
 start=time.monotonic();max_rss=0;aborted=False
 with (WORK/(label+'-private.log')).open('wb') as log:
  child=subprocess.Popen(command,stdout=log,stderr=subprocess.STDOUT,env=env)
  try:
   while child.poll() is None:
    try:
     status=dict(line.split(':',1) for line in (Path('/proc')/str(child.pid)/'status').read_text().splitlines() if ':' in line)
     max_rss=max(max_rss,int(status.get('VmRSS','0 kB').split()[0]))
    except (OSError,ValueError):pass
    if (len(health_samples)>=3 and all(not h['ok'] for h in health_samples[-3:])) or max_rss>1536*1024 or time.monotonic()-start>2220:
     child.send_signal(signal.SIGINT);aborted=True;break
    (WORK/'progress.json').write_text(json.dumps({'label':label,'users':count,'elapsed_seconds':round(time.monotonic()-start),'health_samples':len(health_samples),'health_failures':sum(not h['ok'] for h in health_samples),'exports':len(export_samples),'max_generator_rss_mb':round(max_rss/1024,2)}))
    time.sleep(5)
   code=child.wait(timeout=60)
  finally:
   if child.poll() is None:child.send_signal(signal.SIGINT);child.wait(timeout=60)
 summary=json.loads((WORK/(label+'-summary.json')).read_text()) if (WORK/(label+'-summary.json')).exists() else {'metrics':{}}
 metrics=summary['metrics']
 def value(name,key,default=0):
  metric=metrics.get(name,{})
  return metric.get('values',metric).get(key,metric.get('value',default) if key=='rate' else default)
 checked=verify(studies,count)
 result={'label':label,'users':count,'duration_seconds':round(time.monotonic()-start,2),'exit_code':code,'aborted':aborted,
         'completed':value('completed_participants','count'),'http_requests':value('http_reqs','count'),'http_failure_rate':value('http_req_failed','rate'),
         'p95_ms':value('http_req_duration','p(95)'),'p99_ms':value('http_req_duration','p(99)'),
         'terminal_failures':value('terminal_request_failures','count'),'retries':value('retry_attempts','count'),
         'app_p95_ms':value('server_app_ms','p(95)'),'pool_p95_ms':value('server_pool_ms','p(95)'),
         'max_generator_rss_mb':round(max_rss/1024,2),'database':checked}
 results.append(result);(WORK/'results.json').write_text(json.dumps(results,indent=2))
 print(json.dumps(result),flush=True)
 assert code==0 and not aborted and checked['correct'] and result['completed']==count and result['terminal_failures']==0
 assert result['http_failure_rate']==0
threading.Thread(target=health_worker,daemon=True).start()
threading.Thread(target=exports_worker,daemon=True).start()
failure=None
try:
 for label,count in [('smoke-2',2),('soak-600',600)]:run(label,count)
except Exception as e:failure=type(e).__name__
finally:
 done.set()
 with psycopg.connect(values['DATABASE_URL']) as c:
  studies=[x['study_code'] for x in registry];groups=[x['group_link_id'] for x in registry]
  assigned=c.execute('UPDATE experiment_assignments SET active=0 WHERE group_link_id=ANY(%s) AND study_code=ANY(%s)',(groups,studies)).rowcount
  linked=c.execute('UPDATE experiment_group_links SET active=0 WHERE group_link_id=ANY(%s) AND study_code=ANY(%s)',(groups,studies)).rowcount
 summary={'passed':failure is None,'failure_type':failure,'results':results,'health_probes':len(health_samples),'health_failures':sum(not r['ok'] for r in health_samples),
          'export_operations':len(export_samples),'export_failures':sum(not r['ok'] for r in export_samples),'export_details':export_samples,
          'synthetic_assignments_deactivated':assigned,'synthetic_groups_deactivated':linked,'research_rows_deleted':0,'production_requests':0}
 (WORK/'acceptance-summary.json').write_text(json.dumps(summary,indent=2));print(json.dumps(summary),flush=True)
if failure:raise SystemExit(2)
