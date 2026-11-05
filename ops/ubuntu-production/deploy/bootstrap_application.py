"""Initial app deployment, inspected new host/clean database; no old-host access."""
from pathlib import Path
import subprocess,json,os,tarfile,io,pwd,hashlib,secrets,time,urllib.request,shutil,fcntl
assert os.geteuid()==0
os.umask(0o077)
job=Path('/var/lib/barsan-bootstrap/2026-11-05-app');source=job/'source'
assert not Path('/opt/barsan/current').exists() and not Path('/etc/barsan/application.env').exists()
repo=Path('/srv/barsan/repository');sha='a951b5ac90442a97f2ffdeda108f1ba073314e6e'
def run(args,label,timeout=240,env=None):
 p=subprocess.run(args,capture_output=True,text=True,timeout=timeout,env=env)
 out=job/(label+'.log');out.write_text(p.stdout+p.stderr);out.chmod(0o600)
 assert p.returncode==0,'Deployment stopped: '+label
 return p.stdout.strip()
def git(*args):return run(['runuser','-u','barsan-deploy','--','git','-C',str(repo),*args],'git-'+args[0])
assert git('rev-parse','HEAD')==sha and not git('status','--porcelain')
assert run(['runuser','-u','postgres','--','psql','-XAt','-d','barsan_production','-c',"SELECT count(*) FROM pg_tables WHERE schemaname='public'"],'clean-database-check')=='0'
assert json.loads(Path('/var/lib/barsan-backup/status.json').read_text())['last_restore_test']['restore_verified']
for attempt in range(8):
 pre=json.loads(run(['python3','/usr/local/lib/barsan/backup.py','backup'],'pre-deployment-backup'))
 if pre.get('backup_success'):break
 time.sleep(2)
assert pre.get('backup_success'),'No successful pre-deploy backup'
operation_lock=open('/run/lock/barsan-production-ops.lock','a');fcntl.flock(operation_lock,fcntl.LOCK_EX)
releases=Path('/opt/barsan/releases');releases.mkdir(mode=0o755);releases.chmod(0o755)
release=releases/sha;release.mkdir(mode=0o755);release.chmod(0o755)
archive=subprocess.run(['runuser','-u','barsan-deploy','--','git','-C',str(repo),'archive',sha],capture_output=True,timeout=60);assert archive.returncode==0
hashes={}
with tarfile.open(fileobj=io.BytesIO(archive.stdout)) as tar:
 for member in tar.getmembers():
  parts=Path(member.name).parts
  include=(len(parts)==1 and member.name.endswith('.py')) or (parts and parts[0]=='app') or member.name=='requirements.txt'
  if not include or member.isdir():continue
  assert member.isfile() and not member.name.startswith('/') and '..' not in parts
  target=release/member.name;target.parent.mkdir(mode=0o755,parents=True,exist_ok=True)
  for parent in [target.parent,*target.parent.parents]:
   if parent==release.parent:break
   parent.chmod(0o755)
  body=tar.extractfile(member).read();target.write_bytes(body);target.chmod(0o644);hashes[member.name]=hashlib.sha256(body).hexdigest()
assert all(name in hashes for name in ['main.py','barsan_experiment.py','request_metrics.py','client_timing.py','bulk_exports.py','task_records.py','app/index.html','app/persistence.js','app/task_timing.js','app/admin-links.html'])
(release/'.bulk-export.lock').symlink_to('/var/lib/barsan/bulk-export.lock')
run(['/opt/barsan/venvs/runtime-20261105-v1/bin/python','-m','pip','--isolated','--disable-pip-version-check','install','--dry-run','--no-index','--find-links','/var/cache/barsan/wheels-runtime-20261105-v1','-r',str(release/'requirements.txt')],'runtime-requirements')
app=pwd.getpwnam('barsan-app');etc=Path('/etc/barsan')
token=secrets.token_urlsafe(48)
key=subprocess.check_output(['/opt/barsan/venvs/runtime-20261105-v1/bin/python','-c','from cryptography.fernet import Fernet;import sys;sys.stdout.buffer.write(Fernet.generate_key())'])
for name,body in [('invitation.key',key+b'\n'),('application.env',('ADMIN_TOKEN='+token+'\nSTUDY_CODE=main-study\nSTUDY_NAME=BarSan Experiment\nPUBLIC_EXPERIMENT_URL=https://experiment.barsun4818.ir/dce/\nINVITATION_TOKEN_KEY_FILE=/etc/barsan/invitation.key\nDB_POOL_MIN_SIZE=1\nDB_POOL_MAX_SIZE=4\nDB_POOL_TIMEOUT=5\nDB_POOL_MAX_WAITING=128\nDB_CONNECT_TIMEOUT=5\nBARSAN_SYNC_THREADS=8\nBARSAN_REQUEST_METRICS=1\n').encode())]:
 p=etc/name;assert not p.exists();p.write_bytes(body);p.chmod(0o640);os.chown(p,0,app.pw_gid)
guard=Path('/usr/local/lib/barsan/start_guard.py');assert not guard.exists();shutil.copyfile(source/'start_guard.py',guard);guard.chmod(0o755)
metadata=Path('/opt/barsan/current-release.json');metadata.write_text(json.dumps({'commit':sha,'runtime':'runtime-20261105-v1','canonical_url':'https://experiment.barsun4818.ir/dce/','files':hashes},indent=2));metadata.chmod(0o644)
Path('/opt/barsan/current').symlink_to(release)
unit=Path('/etc/systemd/system/barsan-production.service');assert not unit.exists();shutil.copyfile(source/unit.name,unit);unit.chmod(0o644)
run(['systemd-analyze','verify',str(unit)],'service-syntax')
run(['systemctl','daemon-reload'],'daemon-reload');run(['systemctl','enable','--now','barsan-production.service'],'start-application')
def ready():
 try:
  with urllib.request.urlopen('http://127.0.0.1:8765/api/health',timeout=5) as r:d=json.load(r)
  return d.get('database_connected') is True and d.get('database')=='postgresql'
 except Exception:return False
for _ in range(20):
 if ready():break
 time.sleep(1)
assert ready(),'Loopback app health failed; public URL remains maintenance'
vhost=Path('/etc/nginx/sites-available/barsan-production');original=vhost.read_text();(job/'nginx-before.conf').write_text(original);(job/'nginx-before.conf').chmod(0o600)
old='''    location /dce/ {
        default_type text/plain;
        add_header Retry-After 300 always;
        return 503 "Application deployment is not complete.\\n";
    }'''
new='''    location /dce/ {
        proxy_pass http://127.0.0.1:8765/;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto https;
        proxy_set_header X-Forwarded-For $remote_addr;
        proxy_set_header Connection "";
        proxy_connect_timeout 3s;
        proxy_read_timeout 60s;
        client_max_body_size 2m;
        proxy_hide_header Server;
    }'''
assert original.count(old)==1,'Existing vhost differs; inspect before changing'
vhost.write_text(original.replace(old,new))
try:
 run(['nginx','-t'],'nginx-proxy-syntax');run(['systemctl','reload','nginx'],'nginx-proxy-reload')
 with urllib.request.urlopen('https://experiment.barsun4818.ir/dce/api/health',timeout=15) as r:assert json.load(r)['database_connected']
except Exception:
 vhost.write_text(original);run(['nginx','-t'],'rollback-nginx-syntax');run(['systemctl','reload','nginx'],'rollback-maintenance');raise
fcntl.flock(operation_lock,fcntl.LOCK_UN)
run(['python3','/usr/local/lib/barsan/backup.py','backup'],'post-deployment-backup')
run(['python3','/usr/local/lib/barsan/backup.py','restore-test'],'post-deployment-recovery-test')
result={'application_deployed':True,'commit':sha,'canonical_url':'https://experiment.barsun4818.ir/dce/','clean_database_selected':True,'old_host_changes':False,'database_imported':False,'off_server_backups':False,'file_count':len(hashes),'local_recovery_verified':True}
(job/'record.json').write_text(json.dumps(result,indent=2));(job/'record.json').chmod(0o600)
print(json.dumps(result))
