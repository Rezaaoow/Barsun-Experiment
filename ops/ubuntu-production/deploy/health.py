"""Local status only; does not claim external alerting or disaster recovery."""
from pathlib import Path
import json,subprocess,time,urllib.request,shutil,os
checks={}
try:
 with urllib.request.urlopen('http://127.0.0.1:8765/api/health',timeout=5) as response:
  d=json.load(response);checks['application']=response.status==200 and d.get('database_connected') is True and d.get('database')=='postgresql'
except Exception:checks['application']=False
try:
 backup=json.loads(Path('/var/lib/barsan-backup/status.json').read_text());checks['backup_fresh']=time.time()-backup.get('last_success_epoch',0)<720;checks['backup_last_result']=backup.get('last_result')=='success'
except Exception:checks['backup_fresh']=checks['backup_last_result']=False
checks['backup_volume_mounted']=os.path.ismount('/var/backups/barsan/repository')
checks['root_reserve']=shutil.disk_usage('/').free>=8*1024**3
checks['backup_volume_reserve']=checks['backup_volume_mounted'] and shutil.disk_usage('/var/backups/barsan/repository').free>=512*1024**2
result={'checked_at_epoch':time.time(),'healthy':all(checks.values()),'checks':checks,'off_server_backup':False,'external_alerting':False}
tmp=Path('/var/lib/barsan-operations/health.tmp');tmp.write_text(json.dumps(result,indent=2));tmp.chmod(0o600);tmp.replace('/var/lib/barsan-operations/health.json')
print(json.dumps(result));raise SystemExit(0 if result['healthy'] else 1)
