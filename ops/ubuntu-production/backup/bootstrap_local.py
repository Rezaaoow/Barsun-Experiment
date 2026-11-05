"""Fresh bounded backup installation on the inspected NEW Production server."""
from pathlib import Path
from datetime import datetime,timezone
import subprocess,json,os,secrets,shutil,hashlib
assert os.geteuid()==0
source=Path('/var/lib/barsan-bootstrap/2026-11-05-backup/source')
job=source.parent
assert not Path('/etc/barsan-backup').exists()
assert not Path('/var/lib/barsan-backup').exists()
assert not Path('/usr/local/lib/barsan').exists()
assert shutil.disk_usage('/').free>16*1024**3
assert subprocess.check_output(['runuser','-u','postgres','--','psql','-XAt','-d','barsan_production','-c',"SELECT count(*) FROM pg_tables WHERE schemaname='public'"],text=True).strip()=='0'
os.umask(0o077)
def run(command,label,timeout=240,env=None):
 p=subprocess.run(command,capture_output=True,text=True,timeout=timeout,env=env)
 (job/(label+'.log')).write_text(p.stdout+p.stderr);(job/(label+'.log')).chmod(0o600)
 assert p.returncode==0,'Backup setup stopped: '+label
 return p.stdout.strip()
for directory in ['/etc/barsan-backup','/var/lib/barsan-backup','/var/backups/barsan','/var/backups/barsan/repository','/run/barsan-backup']:
 Path(directory).mkdir(mode=0o700,parents=True,exist_ok=False)
scripts=Path('/usr/local/lib/barsan');scripts.mkdir(mode=0o755);scripts.chmod(0o755)
for name in ['backup.py','capture_database.py','backup_failure.py']:
 p=scripts/name;shutil.copyfile(source/name,p);p.chmod(0o755 if name=='capture_database.py' else 0o750)
password=Path('/etc/barsan-backup/password');password.write_text(secrets.token_urlsafe(48)+'\n');password.chmod(0o600)
image=Path('/var/lib/barsan-backup/repository.ext4')
run(['fallocate','-l',str(4*1024**3),str(image)],'allocate-4g-volume');image.chmod(0o600)
run(['mkfs.ext4','-q','-F','-E','nodiscard','-m','0','-L','barsan-backup',str(image)],'format-new-backup-image')
for name in ['var-backups-barsan-repository.mount','barsan-backup.service','barsan-backup.timer']:
 target=Path('/etc/systemd/system')/name;assert not target.exists();shutil.copyfile(source/name,target);target.chmod(0o644)
journals=Path('/etc/systemd/journald@barsan.conf.d');journals.mkdir(mode=0o755)
(journals/'limits.conf').write_text('[Journal]\nStorage=persistent\nSystemMaxUse=64M\nRuntimeMaxUse=16M\nMaxRetentionSec=7day\n');(journals/'limits.conf').chmod(0o644)
run(['systemd-analyze','verify','/etc/systemd/system/barsan-backup.service','/etc/systemd/system/barsan-backup.timer','/etc/systemd/system/var-backups-barsan-repository.mount'],'systemd-syntax')
run(['systemctl','daemon-reload'],'daemon-reload')
run(['systemctl','enable','--now','var-backups-barsan-repository.mount'],'mount-capped-volume')
mount=Path('/var/backups/barsan/repository');mount.chmod(0o700);assert os.path.ismount(mount)
env={k:v for k,v in os.environ.items() if not k.startswith(('RESTIC_','AWS_'))};env.update(RESTIC_REPOSITORY=str(mount/'restic'),RESTIC_PASSWORD_FILE=str(password),RESTIC_CACHE_DIR=str(mount/'cache'))
run(['restic','init'],'init-encrypted-repository',env=env)
run(['systemctl','start','barsan-backup.service'],'initial-service-backup')
run(['python3',str(scripts/'backup.py'),'restore-test'],'initial-recovery-drill')
run(['systemctl','enable','--now','barsan-backup.timer'],'enable-five-minute-timer')
r={'completed_at_utc':datetime.now(timezone.utc).isoformat(),'status':'local_backup_and_recovery_verified','repository_volume_cap_bytes':4*1024**3,'root_free_bytes_after':shutil.disk_usage('/').free,'root_reserve_bytes':8*1024**3,'off_server':False,'clean_new_database':True,'old_host_changes':False,'backup_status':json.loads(Path('/var/lib/barsan-backup/status.json').read_text())}
(job/'record.json').write_text(json.dumps(r,indent=2));(job/'record.json').chmod(0o600)
print(json.dumps(r))
