"""Frozen old-Production app release. Parallel loopback handover; no dependency/env replacement."""
from pathlib import Path
import ast,fcntl,hashlib,json,os,shutil,socket,subprocess,sys,tarfile,time,urllib.request
import psycopg
from psycopg.conninfo import conninfo_to_dict
os.umask(0o077)
commit,bundle_hash=sys.argv[1:]
assert len(commit)==40
root=Path('/home/myexperi/lifetrade');native=root/'native-asgi';python='/home/myexperi/virtualenv/lifetrade/3.12/bin/python'
job=root/'deployments'/('registration-'+commit[:12]);source=job/'runtime'
old=json.loads((root/'current-release.json').read_text());assert old['loopback_port']==8767
sha=lambda p:hashlib.sha256(p.read_bytes()).hexdigest()
lock=open(native/'registration-release.lock','a');fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
assert sha(job/'bundle.tar.gz')==bundle_hash and not source.exists()
source.mkdir(mode=0o700)
with tarfile.open(job/'bundle.tar.gz') as archive:
 for m in archive.getmembers():
  assert m.isfile() and not m.issym() and not m.islnk() and (source/m.name).resolve().is_relative_to(source.resolve())
  archive.extract(m,source,filter='data')
manifest=json.loads((source/'runtime-manifest.json').read_text());expected=manifest['files'];assert manifest['source_commit']==commit
assert set(old['runtime_files']).issubset(expected)
for name,digest in expected.items():
 if not (source/name).exists():
  previous=Path(old['runtime_source'])/name;assert sha(previous)==digest
  target=source/name;target.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(previous,target)
 assert sha(source/name)==digest
assert (source/'requirements.txt').read_bytes()==(Path(old['runtime_source'])/'requirements.txt').read_bytes()
backup=job/'backup';backup.mkdir(mode=0o700)
paths={'launcher':native/'launch_native.py','supervisor':native/'supervisor.ini','release':root/'current-release.json','env':root/'config/app.env','proxy':Path('/home/myexperi/public_html/lifetrade/.htaccess')}
for name,path in paths.items():shutil.copy2(path,backup/name)
values={}
for line in paths['env'].read_text().splitlines():
 if line.strip() and not line.lstrip().startswith('#') and '=' in line:
  k,v=line.split('=',1);values[k.strip()]=v.strip()
info=conninfo_to_dict(values['DATABASE_URL']);assert info['dbname']=='myexperi_lifetrade_study'
pgenv=os.environ.copy()
for field,key in [('host','PGHOST'),('port','PGPORT'),('dbname','PGDATABASE'),('user','PGUSER'),('password','PGPASSWORD')]:
 if field in info:pgenv[key]=info[field]
def run(args,**kwargs):
 r=subprocess.run(args,capture_output=True,timeout=120,**kwargs)
 if r.returncode:raise RuntimeError('private host operation failed: '+Path(args[0]).name)
 return r.stdout
run(['pg_dump','-Fc','--no-owner','--no-acl','-f',str(job/'database-before.dump')],env=pgenv)
run(['pg_restore','--list',str(job/'database-before.dump')])
assert (job/'database-before.dump').stat().st_size>0
# Record stable preexisting research rows without exposing their content.
def digest_data():
 result={}
 with psycopg.connect(values['DATABASE_URL']) as db:
  tables=[r[0] for r in db.execute("SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename")]
  for table in tables:
   with db.cursor() as cur:
    query=('SELECT participant_id,participant_code,created_at FROM participants ORDER BY participant_id' if table=='participants' else 'SELECT * FROM "'+table+'" ORDER BY 1')
    cur.execute(query);rows=cur.fetchall();result[table]={'count':len(rows),'sha256':hashlib.sha256(repr(rows).encode()).hexdigest()}
 return result
before=digest_data();(backup/'data-digests.json').write_text(json.dumps(before))
launcher=(backup/'launcher').read_text();old_source=repr(old['runtime_source']);assert launcher.count(old_source)==1
launcher=launcher.replace(old_source,repr(str(source)))
matches=[]
for n in ast.walk(ast.parse(launcher)):
 if isinstance(n,ast.Dict):
  try:v=ast.literal_eval(n)
  except Exception:continue
  if v==old['runtime_files']:matches.append(ast.get_source_segment(launcher,n))
assert len(matches)==1;launcher=launcher.replace(matches[0],repr(expected));compile(launcher,'launcher','exec')
assert launcher.count("'8767'")==1
candidate=launcher.replace("'8767'","'8768'")
with socket.socket() as probe:probe.bind(('127.0.0.1',8768))
(job/'launch_candidate.py').write_text(candidate)
conf=job/'candidate.ini'
conf.write_text(f'''[unix_http_server]
file={job}/candidate.sock
chmod=0600
[supervisord]
logfile={job}/supervisor.log
logfile_maxbytes=2MB
logfile_backups=1
pidfile={job}/supervisord.pid
childlogdir={job}
umask=0077
[rpcinterface:supervisor]
supervisor.rpcinterface_factory=supervisor.rpcinterface:make_main_rpcinterface
[supervisorctl]
serverurl=unix://{job}/candidate.sock
[program:candidate]
command={python} {job}/launch_candidate.py
directory={source}
autostart=true
autorestart=true
startsecs=3
stopasgroup=true
killasgroup=true
stopwaitsecs=35
redirect_stderr=true
stdout_logfile={job}/candidate.log
stdout_logfile_maxbytes=4MB
stdout_logfile_backups=1
''')
ctl=[str(Path(python).parent/'supervisorctl'),'-c',str(native/'supervisor.ini')]
cctl=[str(Path(python).parent/'supervisorctl'),'-c',str(conf)]
proxy=paths['proxy'].read_bytes();assert proxy.count(b'http://127.0.0.1:8767/$1')==1
switched=False
changed=False
def atomic(path,body):
 tmp=path.with_name(path.name+'.registration-tmp');assert not tmp.exists();tmp.write_bytes(body);tmp.chmod(path.stat().st_mode&0o777);os.replace(tmp,path)
def health(port):
 for _ in range(40):
  try:
   with urllib.request.urlopen('http://127.0.0.1:'+str(port)+'/api/health',timeout=3) as response:data=json.load(response)
   with urllib.request.urlopen('http://127.0.0.1:'+str(port)+'/',timeout=3) as response:html=response.read()
   if data.get('database_connected') and b'participant-first-name' in html and b'utSelectOpponent' in html:return
  except Exception:pass
  time.sleep(1)
 raise RuntimeError('candidate health failed')
try:
 run([str(Path(python).parent/'supervisord'),'-c',str(conf)]);health(8768)
 after=digest_data();assert before==after,'Existing data changed during preparation; investigate without replacing live workers'
 atomic(paths['proxy'],proxy.replace(b'http://127.0.0.1:8767/$1',b'http://127.0.0.1:8768/$1'));switched=True
 with urllib.request.urlopen('https://my-experiment.ir/lifetrade/',timeout=15) as response:assert b'participant-first-name' in response.read()
 time.sleep(35)
 changed=True
 run(ctl+['stop','barsan-native']);atomic(paths['launcher'],launcher.encode())
 run(ctl+['start','barsan-native']);health(8767)
 atomic(paths['proxy'],proxy);switched=False
 time.sleep(35)
 release=dict(old,commit_sha=commit,runtime_source=str(source),runtime_files=expected,git_main_merge_commit=commit,loopback_port=8767,deployed_at=time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()))
 atomic(paths['release'],json.dumps(release,indent=2).encode())
 for name in expected:
  target=root/'repo'/name
  if not target.is_file() or sha(target)!=expected[name]:target.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(source/name,target)
 assert paths['env'].read_bytes()==(backup/'env').read_bytes()
 assert paths['supervisor'].read_bytes()==(backup/'supervisor').read_bytes()
 assert paths['proxy'].read_bytes()==(backup/'proxy').read_bytes()
 result={'deployed':commit,'parallel_handover':True,'environment_unchanged':True,'proxy_restored':True,'supervisor_unchanged':True,'preexisting_data_verified':True,'backup_verified':True,'runtime_file_count':len(expected)}
 (job/'result.json').write_text(json.dumps(result));print(json.dumps(result))
except Exception:
 if changed:
  subprocess.run(ctl+['stop','barsan-native'],capture_output=True,timeout=60)
  atomic(paths['launcher'],(backup/'launcher').read_bytes());run(ctl+['start','barsan-native'])
  with urllib.request.urlopen('http://127.0.0.1:8767/api/health',timeout=15) as response:assert json.load(response)['database_connected']
 if switched:atomic(paths['proxy'],proxy);switched=False
 raise
finally:
 if not switched:
  try:run(cctl+['stop','candidate']);run(cctl+['shutdown'])
  except Exception:pass
