"""Retire only the existing Staging entry/workers; preserve all archived files/data."""
from pathlib import Path
import hashlib,json,os,shutil,subprocess,time,urllib.request
os.umask(0o077)
root=Path('/home/myexperi/lifetrade-staging');native=root/'native-asgi-trial-20261102'
production=Path('/home/myexperi/lifetrade')
protected=[production/'config/app.env',production/'native-asgi/supervisor.ini',production/'native-asgi/launch_native.py',production/'current-release.json',Path('/home/myexperi/public_html/lifetrade/.htaccess')]
sha=lambda p:hashlib.sha256(p.read_bytes()).hexdigest()
before={str(p):sha(p) for p in protected}
job=root/'retirement-20261105';assert not job.exists();job.mkdir(mode=0o700)
configs=[native/'supervisor.ini',Path('/home/myexperi/public_html/staging/.htaccess'),Path('/home/myexperi/public_html/staging/lifetrade/.htaccess'),Path('/home/myexperi/public_html/staging.my-experiment.ir/.htaccess')]
for i,p in enumerate(configs):shutil.copy2(p,job/('config-'+str(i)))
cron=subprocess.run(['crontab','-l'],capture_output=True,text=True);assert cron.returncode==0
(job/'crontab.before').write_text(cron.stdout)
lines=cron.stdout.splitlines();stage=[line for line in lines if not line.lstrip().startswith('#') and '/lifetrade-staging/' in line]
assert len(stage)==1 and stage[0].startswith('@reboot ') and 'supervisord -c '+str(native/'supervisor.ini') in stage[0]
retained='\n'.join(line for line in lines if line not in stage)+'\n'
def run(cmd,**kwargs):
 r=subprocess.run(cmd,capture_output=True,text=True,timeout=60,**kwargs)
 if r.returncode:raise RuntimeError('retirement operation failed: '+Path(cmd[0]).name)
 return r.stdout
# Place the gone rule before any legacy Passenger/proxy rule, including direct child entry.
for p in configs[1:]:
 text=p.read_text();p.write_text('RewriteEngine On\nRewriteRule ^ - [G,L]\n'+text)
conf=configs[0];text=conf.read_text();assert text.count('autostart=true')==1 and text.count('autorestart=true')==1
conf.write_text(text.replace('autostart=true','autostart=false').replace('autorestart=true','autorestart=false'))
run(['crontab','-'],input=retained)
ctl=['/home/myexperi/virtualenv/lifetrade-staging/3.12/bin/supervisorctl','-c',str(conf)]
run(ctl+['stop','barsan-native']);run(ctl+['shutdown'])
assert all(sha(Path(p))==value for p,value in before.items())
with urllib.request.urlopen('https://my-experiment.ir/lifetrade/api/health',timeout=15) as response:
 assert json.load(response)['database_connected'] is True
result={'retired':True,'public_entry':'HTTP 410','worker_stopped':True,'autostart_removed':True,'data_deleted':False,'production_files_unchanged':True,'at_epoch':time.time()}
(job/'result.json').write_text(json.dumps(result,indent=2));print(json.dumps(result))
