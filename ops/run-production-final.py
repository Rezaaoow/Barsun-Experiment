"""Operator Mac coordinator. Synthetic NEW Production only; no secrets in output."""
import json,os,pathlib,ssl,subprocess,sys,time,urllib.request
import certifi
BASE='https://experiment.barsun4818.ir/dce'
ROOT=pathlib.Path('/Users/rezaaoow/barsan-loadtest/final-acceptance-20261105')
SCRIPT=pathlib.Path(__file__).resolve().with_name('k6-production-final.js')
SSH=['ssh','-o','BatchMode=yes','-o','IdentitiesOnly=yes','-o','StrictHostKeyChecking=yes','-o','ForwardAgent=no','-o','ConnectTimeout=10','-i','/Users/rezaaoow/.ssh/id_ed25519_server','barsan-ops@37.152.183.1']
cohort=sys.argv[1];assert cohort.replace('-','').isalnum()
users=int(sys.argv[2]);mode=sys.argv[3] if len(sys.argv)>3 else 'paced'
distributed=mode=='distributed';burst=mode=='burst';fast=mode=='fast';folder=ROOT/cohort;folder.mkdir(mode=0o700,exist_ok=True)
fixture=folder/'fixture.json';assert fixture.exists();assert json.loads(fixture.read_text())['synthetic']
local_users=users//2 if distributed else users
if distributed:
    subprocess.run(SSH+['mkdir -p /home/barsan-ops/final-load/'+cohort],check=True,capture_output=True)
    subprocess.run(['scp',*SSH[1:-1],str(fixture),str(SCRIPT),SSH[-1]+':/home/barsan-ops/final-load/'+cohort+'/'],check=True,capture_output=True)
    remote=f"cd /home/barsan-ops/final-load/{cohort} && chmod 600 fixture.json && BASE_URL={BASE} FIXTURE_FILE=fixture.json USERS={users//2} OFFSET={local_users} /home/barsan-ops/final-load/bin/k6 run --quiet --summary-export=summary.json k6-production-final.js > private.log 2>&1"
    server_log=open(folder/'server-ssh.log','w');server=subprocess.Popen(SSH+[remote],stdout=server_log,stderr=subprocess.STDOUT)
else:server=None
log=open(folder/'private-k6.log','w');env={**os.environ,'BASE_URL':BASE,'FIXTURE_FILE':str(fixture),'USERS':str(local_users),'OFFSET':'0','MODE':'burst' if burst else 'journey','FAST':'1' if fast else '0'}
local=subprocess.Popen(['/opt/homebrew/bin/k6','run','--quiet','--summary-export='+str(folder/'summary.json'),str(SCRIPT)],env=env,stdout=log,stderr=subprocess.STDOUT)
context=ssl.create_default_context(cafile=certifi.where());health=[];bad=0;start=time.time();aborted=False
try:
    while local.poll() is None or (server and server.poll() is None):
        point={'elapsed_s':round(time.time()-start,1)}
        try:
            begin=time.monotonic()
            with urllib.request.urlopen(BASE+'/api/health',timeout=6,context=context) as r:body=json.load(r);point['http']=r.status
            point['health_ms']=round((time.monotonic()-begin)*1000,2);good=point['http']==200 and body.get('database_connected')
        except Exception as e:point['health_error']=type(e).__name__;good=False
        bad=0 if good else bad+1
        sample=subprocess.run(SSH+["python3 - <<'EOF'\nimport json,pathlib,shutil\nm={l.split(':')[0]:int(l.split()[1]) for l in pathlib.Path('/proc/meminfo').read_text().splitlines() if l.startswith(('MemAvailable:','MemTotal:'))}\nprint(json.dumps({'root_free_bytes':shutil.disk_usage('/').free,'mem_available_kb':m['MemAvailable'],'load':pathlib.Path('/proc/loadavg').read_text().split()[:3]}))\nEOF"],capture_output=True,text=True,timeout=15)
        if sample.returncode==0:point.update(json.loads(sample.stdout))
        health.append(point);(folder/'health-samples.json').write_text(json.dumps(health,indent=2))
        if bad>=3 or point.get('root_free_bytes',10**12)<8*1024**3 or point.get('mem_available_kb',10**9)<300000:
            aborted=True;local.terminate()
            if server:server.terminate()
            break
        time.sleep(5)
    local.wait(timeout=30)
    if server:
        server.wait(timeout=30)
        subprocess.run(['scp',*SSH[1:-1],SSH[-1]+':/home/barsan-ops/final-load/'+cohort+'/summary.json',str(folder/'server-summary.json')],check=True,capture_output=True)
finally:
    if local.poll() is None:local.terminate()
    if server and server.poll() is None:server.terminate()
    log.close()
if distributed:server_log.close()
result={'cohort':cohort,'users':users,'network_origins':2 if distributed else 1,'mode':mode,'elapsed_s':round(time.time()-start,1),'local_exit':local.returncode,'server_exit':server.returncode if server else None,'aborted':aborted,'health_samples':len(health),'health_failures':sum(p.get('http')!=200 for p in health)}
(folder/'run.json').write_text(json.dumps(result,indent=2));print(json.dumps(result))
raise SystemExit(0 if not aborted and local.returncode==0 and (not server or server.returncode==0) else 1)
