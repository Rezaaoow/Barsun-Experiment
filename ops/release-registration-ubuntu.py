"""Operator-only frozen app release with a temporary parallel worker and proxy drain.

Run as root on the NEW server only. No dependency, secret or database rollback.
The installed privileged release implementation is deliberately not self-updated.
"""
from pathlib import Path
import fcntl,hashlib,importlib.util,io,json,os,re,subprocess,sys,tarfile,time,urllib.request

assert os.geteuid()==0
os.umask(0o077)
commit=sys.argv[1]
assert re.fullmatch('[0-9a-f]{40}',commit)
spec=importlib.util.spec_from_file_location('installed_release','/usr/local/lib/barsan/release.py')
r=importlib.util.module_from_spec(spec);spec.loader.exec_module(r)
old=json.loads((r.ROOT/'current-release.json').read_text())
assert old['canonical_url']=='https://experiment.barsun4818.ir/dce/' and old['commit']!=commit
r.git('fetch','origin')
assert r.git('rev-parse','refs/remotes/origin/production-approved').decode().strip()==commit
r.git('merge-base','--is-ancestor',commit,'refs/remotes/origin/main')
lock=open('/run/lock/barsan-release.lock','a');fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
r.require_backup()
operation=open('/run/lock/barsan-production-ops.lock','a');fcntl.flock(operation,fcntl.LOCK_EX)
job=Path('/var/lib/barsan-operations')/('registration-'+commit[:12]);job.mkdir(mode=0o700)
nginx=Path('/etc/nginx/sites-available/barsan-production');proxy=nginx.read_bytes()
unit=Path('/etc/systemd/system/barsan-registration-candidate.service')
assert not unit.exists()
source=r.ROOT/'releases'/commit;assert not source.exists();source.mkdir(mode=0o755);source.chmod(0o755)
hashes={}
with tarfile.open(fileobj=io.BytesIO(r.git('archive',commit))) as archive:
 for member in archive.getmembers():
  parts=Path(member.name).parts
  if not ((len(parts)==1 and member.name.endswith('.py')) or (parts and parts[0]=='app') or member.name=='requirements.txt') or member.isdir():continue
  assert member.isfile() and '..' not in parts and not member.name.startswith('/')
  body=archive.extractfile(member).read();target=source/member.name
  target.parent.mkdir(parents=True,exist_ok=True,mode=0o755)
  for parent in [target.parent,*target.parent.parents]:
   if parent==source.parent:break
   parent.chmod(0o755)
  target.write_bytes(body);target.chmod(0o644);hashes[member.name]=hashlib.sha256(body).hexdigest()
assert (source/'requirements.txt').read_bytes()==(r.ROOT/'current/requirements.txt').read_bytes()
(source/'.bulk-export.lock').symlink_to('/var/lib/barsan/bulk-export.lock')
metadata=dict(old,commit=commit,files=hashes)
(job/'previous-release.json').write_text(json.dumps(old));(job/'nginx.before').write_bytes(proxy)
primary=Path('/etc/systemd/system/barsan-production.service').read_text()
candidate='\n'.join(line for line in primary.splitlines() if not line.startswith('ExecStartPre=') and line not in ('[Install]','WantedBy=multi-user.target'))
assert candidate.count('--port 8765 ')==1
candidate=candidate.replace('WorkingDirectory=/opt/barsan/current','WorkingDirectory='+str(source)).replace('--port 8765 ','--port 8768 ')
unit.write_text(candidate+'\n');unit.chmod(0o644)
def atomic_bytes(path,body):
 temporary=path.with_name(path.name+'.registration-tmp');assert not temporary.exists();temporary.write_bytes(body);temporary.chmod(path.stat().st_mode&0o777);os.replace(temporary,path)
def health(port):
 for _ in range(40):
  try:
   with urllib.request.urlopen('http://127.0.0.1:'+str(port)+'/api/health',timeout=3) as response:data=json.load(response)
   with urllib.request.urlopen('http://127.0.0.1:'+str(port)+'/',timeout=3) as response:html=response.read()
   if data.get('database_connected') and b'participant-first-name' in html and b'utSelectOpponent' in html:return
  except Exception:pass
  time.sleep(1)
 raise RuntimeError('candidate_not_healthy')
def route(body):
 atomic_bytes(nginx,body);r.command(['nginx','-t']);r.command(['systemctl','reload','nginx'])
changed=False
try:
 r.command(['systemctl','daemon-reload']);r.command(['systemctl','start',unit.stem])
 health(8768)
 assert proxy.count(b'proxy_pass http://127.0.0.1:8765/;')==1
 route(proxy.replace(b'proxy_pass http://127.0.0.1:8765/;',b'proxy_pass http://127.0.0.1:8768/;'))
 # Drain requests already accepted by old nginx workers before replacing canonical workers.
 time.sleep(35)
 r.atomic(r.STATE/'previous-release.json',old);r.switch(metadata);changed=True
 r.command(['systemctl','restart','barsan-production.service']);health(8765)
 route(proxy);time.sleep(35)
 assert nginx.read_bytes()==proxy
 r.atomic(r.STATE/'deployment.json',{'status':'healthy','commit':commit,'previous':old['commit'],'action':'parallel_registration_release','database_rolled_back':False,'at_epoch':time.time()})
 (job/'result.json').write_text(json.dumps({'passed':True,'commit':commit,'parallel_handover':True,'nginx_restored':True,'dependencies_unchanged':True}))
 print(json.dumps({'deployed':commit,'parallel_handover':True,'nginx_restored':True}))
except Exception:
 if changed:r.switch(old);r.command(['systemctl','restart','barsan-production.service'])
 route(proxy)
 raise
finally:
 # Candidate is stopped only after routing is restored to canonical port.
 if nginx.read_bytes()==proxy:
  r.command(['systemctl','stop',unit.stem]);unit.unlink();r.command(['systemctl','daemon-reload'])
fcntl.flock(operation,fcntl.LOCK_UN)
r.require_backup()
