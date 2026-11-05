"""Local-only encrypted Production backups. Root-only; never contacts old hosting."""
from pathlib import Path
from datetime import datetime,timezone
import argparse,fcntl,hashlib,json,os,resource,shutil,subprocess,sys,tarfile,time,uuid

MOUNT=Path('/var/backups/barsan/repository')
STATE=Path('/var/lib/barsan-backup')
PASSWORD=Path('/etc/barsan-backup/password')
PYTHON='/opt/barsan/venvs/runtime-20261105-v1/bin/python'
HOST='barsan-production'
TAG='barsan-production'
GIB=1024**3
RESERVE=8*GIB
MIN_VOLUME_FREE=512*1024**2
MAX_CAPTURE=192*1024**2
STAGE=Path('/run/barsan-backup/capture')

class BackupError(Exception):pass

def require(value,reason):
 if not value:raise BackupError(reason)

def run(command,timeout=240,output=None):
 result=subprocess.run(command,stdout=output if output is not None else subprocess.PIPE,stderr=subprocess.PIPE,timeout=timeout)
 require(result.returncode==0,'command_failed_'+Path(command[0]).name)
 return result.stderr if output is not None else result.stdout

def pg(*args,output=None):
 return run(['setpriv','--reuid=postgres','--regid=postgres','--init-groups','--','env','-i','PATH=/usr/bin:/bin','PGHOST=/var/run/postgresql','PGPORT=5432','PGUSER=postgres',*args],output=output)

def psql(statement,database='postgres'):
 return pg('psql','-XAt','-v','ON_ERROR_STOP=1','-d',database,'-c',statement).decode().strip()

def atomic(path,data):
 tmp=path.with_name(path.name+'.tmp');tmp.write_text(json.dumps(data,indent=2));tmp.chmod(0o600);os.replace(tmp,path)

def status():
 path=STATE/'status.json'
 return json.loads(path.read_text()) if path.exists() else {'off_server':False}

def preflight(require_root_reserve=True):
 require(os.geteuid()==0,'root_required')
 require(os.path.ismount(MOUNT),'backup_mount_missing')
 require(MOUNT.stat().st_uid==0 and MOUNT.stat().st_mode & 0o077==0,'backup_mount_permissions')
 require(PASSWORD.is_file() and PASSWORD.stat().st_uid==0 and PASSWORD.stat().st_mode & 0o777==0o600,'password_permissions')
 require(shutil.disk_usage(MOUNT).total<=4*GIB,'backup_volume_exceeds_cap')
 if require_root_reserve:require(shutil.disk_usage('/').free>=RESERVE,'root_space_reserve')
 require(run(['findmnt','-no','FSTYPE','/run']).decode().strip()=='tmpfs','capture_requires_tmpfs')
 env={key:value for key,value in os.environ.items() if not key.startswith(('RESTIC_','AWS_','PG'))}
 env.update(RESTIC_REPOSITORY=str(MOUNT/'restic'),RESTIC_PASSWORD_FILE=str(PASSWORD),RESTIC_CACHE_DIR=str(MOUNT/'cache'))
 return env

def restic(*args,timeout=240):
 result=subprocess.run(['restic',*args],env=preflight(False),capture_output=True,timeout=timeout)
 require(result.returncode==0,'restic_operation_failed')
 return result.stdout

def snapshots():
 rows=json.loads(restic('snapshots','--json','--host',HOST,'--tag',TAG)) or []
 return sorted(rows,key=lambda row:row['time'])

def retention():
 before=snapshots();require(before,'no_snapshot_to_retain')
 latest=before[-1]['id']
 restic('forget','--host',HOST,'--tag',TAG,'--group-by','host,paths,tags','--keep-last','2','--keep-within','6h','--keep-hourly','24','--keep-daily','7','--keep-weekly','4','--json')
 require(any(row['id']==latest for row in snapshots()),'latest_snapshot_not_retained')
 restic('prune','--max-unused','5%','--max-repack-size','128M')
 restic('check')

def clean_stage():
 if STAGE.exists():
  require(not STAGE.is_symlink() and STAGE.stat().st_uid==0,'unsafe_capture_path')
  shutil.rmtree(STAGE)

def capture():
 preflight()
 if shutil.disk_usage(MOUNT).free<MIN_VOLUME_FREE+MAX_CAPTURE:
  retention()
 require(shutil.disk_usage(MOUNT).free>=MIN_VOLUME_FREE+MAX_CAPTURE,'backup_volume_capacity')
 clean_stage();STAGE.mkdir(mode=0o700,parents=True)
 metadata={}
 try:
  with (STAGE/'database.dump').open('xb') as out:
   metadata=json.loads(run(['setpriv','--reuid=postgres','--regid=postgres','--init-groups','--',PYTHON,str(Path(__file__).with_name('capture_database.py'))],output=out))
  with (STAGE/'globals.sql').open('xb') as out:pg('pg_dumpall','--globals-only','--no-role-passwords',output=out)
  sources=[Path(p) for p in ['/etc/barsan','/etc/barsan-operations','/etc/postgresql/16/main','/etc/nginx','/etc/letsencrypt','/etc/ssh','/etc/systemd/journald@barsan.conf.d','/etc/systemd/system/var-backups-barsan-repository.mount','/usr/local/lib/barsan','/opt/barsan/current-release.json','/var/cache/barsan/wheels-runtime-20261105-v1'] if Path(p).exists()]
  sources.extend(sorted(Path('/etc/systemd/system').glob('barsan-*.service')))
  sources.extend(sorted(Path('/etc/systemd/system').glob('barsan-*.timer')))
  current=Path('/opt/barsan/current')
  if current.is_symlink():
   resolved=current.resolve();require(resolved.parent==Path('/opt/barsan/releases'),'unexpected_release_path');sources.append(resolved)
  with tarfile.open(STAGE/'configuration.tar','w') as archive:
   for source in sources:archive.add(source,arcname=str(source).lstrip('/'),recursive=True)
  for path in STAGE.iterdir():path.chmod(0o600)
  metadata.update(created_at_utc=datetime.now(timezone.utc).isoformat(),off_server=False,files={p.name:{'bytes':p.stat().st_size,'sha256':hashlib.sha256(p.read_bytes()).hexdigest()} for p in STAGE.iterdir()},release=json.loads(Path('/opt/barsan/current-release.json').read_text()) if Path('/opt/barsan/current-release.json').exists() else None)
  require(sum(p.stat().st_size for p in STAGE.iterdir())<=MAX_CAPTURE,'capture_size_limit')
  atomic(STAGE/'manifest.json',metadata)
  lines=restic('backup','--json','--host',HOST,'--tag',TAG,str(STAGE)).decode().splitlines()
  summary=next(json.loads(line) for line in reversed(lines) if json.loads(line).get('message_type')=='summary')
  snapshot=summary['snapshot_id'];require(snapshot,'snapshot_missing')
  restic('check')
  retention()
  info=status();info.update(last_success_utc=datetime.now(timezone.utc).isoformat(),last_success_epoch=time.time(),last_snapshot=snapshot,last_capture_bytes=sum(p.stat().st_size for p in STAGE.iterdir()),last_result='success',last_failure=None,off_server=False,backup_volume_limit_bytes=4*GIB,backup_volume_free_bytes=shutil.disk_usage(MOUNT).free,root_free_bytes=shutil.disk_usage('/').free,root_reserve_bytes=RESERVE)
  atomic(STATE/'status.json',info)
  return {'backup_success':True,'snapshot':snapshot,'capture_bytes':info['last_capture_bytes'],'backup_volume_free_bytes':info['backup_volume_free_bytes'],'off_server':False}
 finally:clean_stage()

def recovery_test():
 preflight();started=time.monotonic();rows=snapshots();require(rows,'no_backup_for_recovery')
 database_bytes=int(psql("SELECT pg_database_size('barsan_production')"))
 require(shutil.disk_usage('/').free>=RESERVE+2*database_bytes,'recovery_root_space_reserve')
 snapshot=rows[-1]['id'];restic('check','--read-data')
 manifest=json.loads(restic('dump',snapshot,str(STAGE/'manifest.json')))
 require(manifest['database']=='barsan_production','wrong_database_manifest')
 temporary=Path('/run/barsan-backup/recovery-'+uuid.uuid4().hex);temporary.mkdir(mode=0o700)
 name='barsan_recovery_'+uuid.uuid4().hex[:20];marker='barsan-owned-recovery:'+uuid.uuid4().hex;created=False
 try:
  # Every staged file is recovered/decrypted and hash checked, including secrets in configuration.tar.
  for filename,expected in manifest['files'].items():
   require(filename in ['database.dump','globals.sql','configuration.tar'],'unexpected_backup_file')
   output=temporary/filename
   with output.open('xb') as out:
    result=subprocess.run(['restic','dump',snapshot,str(STAGE/filename)],env=preflight(False),stdout=out,stderr=subprocess.PIPE,timeout=240)
   require(result.returncode==0,'restore_file_failed')
   require(output.stat().st_size==expected['bytes'] and hashlib.sha256(output.read_bytes()).hexdigest()==expected['sha256'],'restored_file_hash_mismatch')
  require(psql("SELECT count(*) FROM pg_database WHERE datname='"+name+"'")=='0','recovery_database_exists')
  psql('CREATE DATABASE '+name+' OWNER postgres TEMPLATE template0');created=True
  psql("COMMENT ON DATABASE "+name+" IS '"+marker+"'")
  # Root opens the archive; postgres reads through stdin without permission to read other temporary files.
  with (temporary/'database.dump').open('rb') as inp:
   result=subprocess.run(['setpriv','--reuid=postgres','--regid=postgres','--init-groups','--','env','-i','PATH=/usr/bin:/bin','PGHOST=/var/run/postgresql','PGUSER=postgres','pg_restore','--exit-on-error','--no-owner','--no-privileges','-d',name],stdin=inp,capture_output=True,timeout=240)
  require(result.returncode==0,'database_restore_failed')
  actual_names=psql("SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename",name).splitlines()
  require(set(actual_names)==set(manifest['tables']),'restored_schema_mismatch')
  for table,count in manifest['tables'].items():
   require(table.replace('_','').isalnum(),'unsafe_table_name')
   require(int(psql('SELECT count(*) FROM public."'+table+'"',name))==count,'restored_count_mismatch')
  result={'restore_verified':True,'snapshot':snapshot,'tables_verified':len(actual_names),'file_hashes_verified':True,'elapsed_seconds':round(time.monotonic()-started,2),'off_server':False}
  info=status();info['last_restore_test']=dict(result,verified_at_utc=datetime.now(timezone.utc).isoformat());info['last_restore_result']='success';atomic(STATE/'status.json',info)
  return result
 finally:
  if created:
   require(psql("SELECT pg_get_userbyid(datdba)||':'||shobj_description(oid,'pg_database') FROM pg_database WHERE datname='"+name+"'")=='postgres:'+marker,'recovery_database_cleanup_guard')
   psql('DROP DATABASE '+name)
  shutil.rmtree(temporary)

def main():
 parser=argparse.ArgumentParser();parser.add_argument('action',choices=['backup','check','restore-test','status']);args=parser.parse_args()
 require(os.geteuid()==0,'root_required');os.umask(0o077)
 resource.setrlimit(resource.RLIMIT_FSIZE,(MAX_CAPTURE,MAX_CAPTURE))
 if args.action=='status':print(json.dumps(status()));return
 lock=open('/run/lock/barsan-production-ops.lock','a');os.chmod(lock.name,0o600)
 try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
 except BlockingIOError:print(json.dumps({'skipped':'operations_lock_busy'}));return
 try:
  result=capture() if args.action=='backup' else recovery_test() if args.action=='restore-test' else {'repository_check':bool(restic('check') is not None),'off_server':False}
  print(json.dumps(result))
 except Exception as error:
  info=status();reason=str(error) if isinstance(error,BackupError) else type(error).__name__
  info.update(last_result='failure',last_failure=reason,last_failure_utc=datetime.now(timezone.utc).isoformat(),off_server=False);atomic(STATE/'status.json',info)
  if args.action=='restore-test':info['last_restore_result']='failure';atomic(STATE/'status.json',info)
  print(json.dumps({'backup_operation_failed':True,'reason':reason}),file=sys.stderr);raise SystemExit(1)

if __name__=='__main__':main()
