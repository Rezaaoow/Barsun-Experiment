"""Aggregate only own Staging process resources and connection counts during soak."""
from pathlib import Path
import json,os,subprocess,time
import psycopg
ROOT=Path('/home/myexperi/lifetrade-staging');JOB=ROOT/'deployments/final-85c2158bdb9b';WORK=JOB/'soak-30m'
os.umask(0o077)
values={}
for raw in (ROOT/'config/app.env').read_text().splitlines():
 if raw.strip() and not raw.startswith('#') and '=' in raw:
  k,v=raw.split('=',1);values[k.strip()]=v.strip()
ctl=['/home/myexperi/virtualenv/lifetrade-staging/3.12/bin/supervisorctl','-c',str(ROOT/'native-asgi-trial-20261102/supervisor.ini'),'pid','barsan-native']
samples=[];started=time.monotonic()
while time.monotonic()-started<2400:
 try:
  pid=int(subprocess.run(ctl,capture_output=True,timeout=10,check=True).stdout)
  processes={}
  account_threads=0;account_processes=0
  for path in Path('/proc').iterdir():
   if not path.name.isdigit():continue
   try:
    if path.stat().st_uid!=os.getuid():continue
    data=dict(line.split(':',1) for line in (path/'status').read_text().splitlines() if ':' in line)
    process_id=int(path.name);parent=int(data.get('PPid','0'))
    threads=int(data.get('Threads','0'));rss=int(data.get('VmRSS','0 kB').split()[0])
    account_threads+=threads;account_processes+=1;processes[process_id]=(parent,threads,rss)
   except (OSError,ValueError):continue
  selected={pid}
  for _ in range(4):selected.update(p for p,row in processes.items() if row[0] in selected)
  rss=sum(processes[p][2] for p in selected if p in processes)
  threads=sum(processes[p][1] for p in selected if p in processes)
  with psycopg.connect(values['DATABASE_URL']) as c:
   connections=c.execute("SELECT COUNT(*) FROM pg_stat_activity WHERE datname='myexperi_barsan_staging'").fetchone()[0]
  samples.append({'elapsed_seconds':round(time.monotonic()-started),'native_rss_mb':round(rss/1024,2),
    'native_threads':threads,'native_processes':len(selected),'account_processes':account_processes,'account_threads':account_threads,
    'staging_db_connections_including_monitor':connections})
 except Exception: samples.append({'sample_error':True})
 (WORK/'resources-aggregate.json').write_text(json.dumps(samples,indent=2))
 if (WORK/'acceptance-summary.json').exists():break
 time.sleep(30)
