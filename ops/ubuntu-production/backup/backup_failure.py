"""Record service timeout/OOM failures without dumping stderr or sensitive content."""
from pathlib import Path
from datetime import datetime,timezone
import json,os,sys
reason=os.environ.get('SERVICE_RESULT','success')
if reason!='success':
 path=Path('/var/lib/barsan-backup/status.json')
 info=json.loads(path.read_text()) if path.exists() else {}
 if len(sys.argv)>1 and sys.argv[1]=='restore':info.update(last_restore_result='failure',last_restore_failure='service_'+reason,off_server=False)
 else:info.update(last_result='failure',last_failure='service_'+reason,last_failure_utc=datetime.now(timezone.utc).isoformat(),off_server=False)
 tmp=path.with_suffix('.tmp');tmp.write_text(json.dumps(info,indent=2));tmp.chmod(0o600);tmp.replace(path)
