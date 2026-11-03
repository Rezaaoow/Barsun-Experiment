"""Run an on-host diagnostic; shares the account's CPU/RAM, not production traffic."""
from pathlib import Path
import json, os, resource, signal, subprocess, sys, time, urllib.request
root=Path('/home/myexperi/lifetrade-staging/test-runs/capacity-fix-20261103')
label=sys.argv[1]
assert label in ['600-selfhttps','600-local','400-dual-host']
count=400 if label=='400-dual-host' else 600
base='https://staging.my-experiment.ir/lifetrade' if label in ['600-selfhttps','400-dual-host'] else 'http://127.0.0.1:8766'
fixture=root/('participants-'+label+'.json')
assert fixture.exists()
assert not (root/(label+'-summary.json')).exists()
os.umask(0o077)
def health():
 t=time.monotonic()
 try:
  with urllib.request.urlopen('https://my-experiment.ir/lifetrade/api/health',timeout=8) as r:
   h=json.loads(r.read());return {'at':time.time(),'ok':r.status==200 and h.get('ok') is True and h.get('database_connected') is True,'seconds':time.monotonic()-t}
 except Exception as e:return {'at':time.time(),'ok':False,'error':type(e).__name__,'seconds':time.monotonic()-t}
assert all(health()['ok'] for _ in range(3)),'production baseline is unhealthy'
env=os.environ.copy();env.update(GOMAXPROCS='2',GOMEMLIMIT='1GiB',K6_NO_USAGE_REPORT='true')
soft,hard=resource.getrlimit(resource.RLIMIT_NOFILE)
resource.setrlimit(resource.RLIMIT_NOFILE,(min(4096,hard),hard))
(root/(label+'-start.json')).write_text(json.dumps({'started_at':time.time(),'release':'7af183881410faf5d089efefe44d71d43781d8ce','base':base,'users':count,'generator':'on-host k6 v2.3.0 official','shares_host_resources':True}))
cmd=[str(root/'k6'),'run','--quiet','--summary-trend-stats','avg,min,med,max,p(90),p(95),p(99)','--summary-export',str(root/(label+'-summary.json')),'-e','BASE_URL='+base,'-e','EXPECTED_USERS='+str(count),'-e','PARTICIPANTS_FILE='+str(fixture),str(root/'k6-diagnostic.js')]
with (root/(label+'.log')).open('w') as output, (root/('production-guard-'+label+'.jsonl')).open('w',buffering=1) as guard:
 child=subprocess.Popen(cmd,stdout=output,stderr=subprocess.STDOUT,env=env)
 print('ON_HOST_LOAD_STARTED',label,child.pid,flush=True)
 failures=0;samples=[];begun=time.monotonic()
 while child.poll() is None:
  h=health();guard.write(json.dumps(h)+'\n');failures=0 if h['ok'] else failures+1
  try:
   status=dict(l.split(':',1) for l in (Path('/proc')/str(child.pid)/'status').read_text().splitlines() if ':' in l)
   sample={'at':time.time(),'rss_kb':int(status.get('VmRSS','0 kB').split()[0]),'threads':int(status['Threads'])};samples.append(sample)
   if sample['rss_kb']>1500*1024:child.send_signal(signal.SIGINT);print('STOPPED_GENERATOR_MEMORY_GUARD',flush=True);break
  except (OSError,KeyError):pass
  if failures>=2 or time.monotonic()-begun>780:
   child.send_signal(signal.SIGINT);print('STOPPED_BY_GUARD',flush=True);break
  time.sleep(5)
 code=child.wait(timeout=60)
 (root/(label+'-end.json')).write_text(json.dumps({'finished_at':time.time(),'exit_code':code,'max_generator_rss_mb':max((s['rss_kb'] for s in samples),default=0)/1024,'max_generator_threads':max((s['threads'] for s in samples),default=0)}))
 print('ON_HOST_LOAD_FINISHED',label,code,flush=True)
