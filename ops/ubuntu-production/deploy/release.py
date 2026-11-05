"""Constrained app-only frozen release/rollback. Installed root code is not self-updated."""
from pathlib import Path
import argparse,fcntl,hashlib,io,json,os,re,shutil,subprocess,tarfile,time,urllib.request

REPO=Path('/srv/barsan/repository');ROOT=Path('/opt/barsan');STATE=Path('/var/lib/barsan-operations')
PYTHON='/opt/barsan/venvs/runtime-20261105-v1/bin/python'
def command(args,timeout=120):
 result=subprocess.run(args,capture_output=True,timeout=timeout)
 if result.returncode:raise RuntimeError('operation_failed_'+Path(args[0]).name)
 return result.stdout
def git(*args):return command(['setpriv','--reuid=barsan-deploy','--regid=barsan-deploy','--init-groups','--','env','-u','GIT_SSH_COMMAND','-u','SSH_AUTH_SOCK','GIT_TERMINAL_PROMPT=0','git','-C',str(REPO),*args])
def atomic(path,data):
 temp=path.with_name(path.name+'.tmp');temp.write_text(json.dumps(data,indent=2));temp.chmod(0o644 if path.parent==ROOT else 0o600);os.replace(temp,path)
def healthy():
 for _ in range(25):
  try:
   with urllib.request.urlopen('http://127.0.0.1:8765/api/health',timeout=3) as response:
    data=json.load(response)
   if data.get('database_connected') is True and data.get('database')=='postgresql':return True
  except Exception:pass
  time.sleep(1)
 return False
def switch(metadata):
 link=ROOT/'next-release';assert not link.exists() and not link.is_symlink()
 link.symlink_to(ROOT/'releases'/metadata['commit']);os.replace(link,ROOT/'current');atomic(ROOT/'current-release.json',metadata)
def active_research():
 sql="SELECT count(*) FROM instrument_runs r JOIN study_participations sp USING(participation_id) JOIN experiment_assignments a ON a.study_code=sp.study_code AND a.participant_id=sp.participant_id WHERE r.status='in_progress' AND a.active=1 AND sp.study_code NOT LIKE 'barsan-test-%'"
 return int(command(['setpriv','--reuid=postgres','--regid=postgres','--init-groups','--','env','-i','PATH=/usr/bin:/bin','PGHOST=/var/run/postgresql','PGUSER=postgres','psql','-XAt','-d','barsan_production','-c',sql]).decode().strip())

def require_backup():
 for _ in range(10):
  result=json.loads(command(['python3','/usr/local/lib/barsan/backup.py','backup'],240))
  if result.get('backup_success'):return
  time.sleep(2)
 raise RuntimeError('successful_backup_required')

def main():
 parser=argparse.ArgumentParser();parser.add_argument('action',choices=['poll','deploy','rollback']);parser.add_argument('commit',nargs='?');parser.add_argument('--allow-active',action='store_true');args=parser.parse_args()
 assert os.geteuid()==0;os.umask(0o077)
 lock=open('/run/lock/barsan-release.lock','a')
 try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
 except BlockingIOError:return
 old=json.loads((ROOT/'current-release.json').read_text());target=args.commit
 try:
  if args.action=='rollback':
   assert not target,'Rollback uses the recorded previous release only'
   previous=json.loads((STATE/'previous-release.json').read_text());target=previous['commit']
  else:
   git('fetch','origin')
   approved=git('rev-parse','refs/remotes/origin/production-approved').decode().strip()
   target=target or approved
   assert target==approved,'Only the CI-approved ref can be deployed'
   git('merge-base','--is-ancestor',target,'refs/remotes/origin/main')
  assert re.fullmatch(r'[0-9a-f]{40}',target)
  if target==old['commit']:return
  if active_research() and not args.allow_active:
   atomic(STATE/'deployment.json',{'status':'deferred_active_research','target':target,'at_epoch':time.time()});return
  require_backup()
  operation=open('/run/lock/barsan-production-ops.lock','a');fcntl.flock(operation,fcntl.LOCK_EX)
  if active_research() and not args.allow_active:
   atomic(STATE/'deployment.json',{'status':'deferred_active_research','target':target,'at_epoch':time.time()});return
  if args.action=='rollback':
   metadata=previous;release=ROOT/'releases'/target
   for name,digest in metadata['files'].items():assert hashlib.sha256((release/name).read_bytes()).hexdigest()==digest
  else:
   release=ROOT/'releases'/target;existing=release.exists()
   if existing:assert release.stat().st_uid==0 and not release.is_symlink()
   else:release.mkdir(mode=0o755);release.chmod(0o755)
   hashes={}
   with tarfile.open(fileobj=io.BytesIO(git('archive',target))) as archive:
    for member in archive.getmembers():
     parts=Path(member.name).parts
     if not ((len(parts)==1 and member.name.endswith('.py')) or (parts and parts[0]=='app') or member.name=='requirements.txt') or member.isdir():continue
     assert member.isfile() and not member.name.startswith('/') and '..' not in parts
     out=release/member.name
     body=archive.extractfile(member).read()
     if existing:
      assert out.is_file() and not out.is_symlink() and out.read_bytes()==body,'Existing release contents changed'
      hashes[member.name]=hashlib.sha256(body).hexdigest();continue
     out.parent.mkdir(parents=True,exist_ok=True,mode=0o755)
     for parent in [out.parent,*out.parent.parents]:
      if parent==release.parent:break
      parent.chmod(0o755)
     out.write_bytes(body);out.chmod(0o644);hashes[member.name]=hashlib.sha256(body).hexdigest()
   assert all(name in hashes for name in ['main.py','barsan_experiment.py','request_metrics.py','task_records.py','bulk_exports.py','client_timing.py','app/index.html','app/admin-links.html','app/persistence.js','app/task_timing.js'])
   assert (release/'requirements.txt').read_bytes()==(ROOT/'current'/'requirements.txt').read_bytes(),'Dependency change requires a new validated runtime; no automatic package resolution'
   if not existing:(release/'.bulk-export.lock').symlink_to('/var/lib/barsan/bulk-export.lock')
   metadata={'commit':target,'runtime':old['runtime'],'canonical_url':old['canonical_url'],'files':hashes}
  atomic(STATE/'previous-release.json',old)
  switch(metadata)
  try:
   command(['systemctl','restart','barsan-production.service'])
   assert healthy(),'New release health failed'
  except Exception:
   switch(old);command(['systemctl','restart','barsan-production.service']);assert healthy(),'Code rollback health failed; database was not reverted'
   raise RuntimeError('release_failed_previous_code_restored') from None
  fcntl.flock(operation,fcntl.LOCK_UN)
  require_backup()
  atomic(STATE/'deployment.json',{'status':'healthy','commit':target,'previous':old['commit'],'action':args.action,'at_epoch':time.time(),'database_rolled_back':False})
  # Keep current + previous + one other verified release; backup/health succeeded first.
  protected={target,old['commit']};others=sorted((p for p in (ROOT/'releases').iterdir() if p.is_dir() and re.fullmatch(r'[0-9a-f]{40}',p.name) and p.name not in protected),key=lambda p:p.stat().st_mtime,reverse=True)
  for stale in others[1:]:
   assert stale.parent==ROOT/'releases' and stale.stat().st_uid==0 and not stale.is_symlink();shutil.rmtree(stale)
  print(json.dumps({'deployed':target,'healthy':True,'database_rolled_back':False}))
 except Exception as error:
  atomic(STATE/'deployment.json',{'status':'failure','error_type':type(error).__name__,'at_epoch':time.time()});raise SystemExit('Release operation failed; inspect protected deployment status')

if __name__=='__main__':main()
