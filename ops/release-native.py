"""Deploy a frozen release; host-only secrets/backups, independent native services.

Run only after exact-SHA CI. Production mode additionally requires completed
Staging acceptance evidence for the same runtime hashes. No load tests run here.
"""
from pathlib import Path
from datetime import datetime, timezone
import argparse, hashlib, json, os, shlex, shutil, subprocess, tarfile, time
import urllib.request
import urllib.parse
import psycopg
from psycopg.conninfo import conninfo_to_dict

os.umask(0o077)
parser=argparse.ArgumentParser()
parser.add_argument('environment',choices=['staging','production'])
parser.add_argument('commit')
parser.add_argument('bundle_sha256')
a=parser.parse_args()
assert len(a.commit)==40 and all(c in '0123456789abcdef' for c in a.commit)
staging=a.environment=='staging'
ROOT=Path('/home/myexperi/lifetrade-staging' if staging else '/home/myexperi/lifetrade')
JOB=ROOT/'deployments'/('final-'+a.commit[:12])
SOURCE=JOB/'runtime'
NATIVE=ROOT/('native-asgi-trial-20261102' if staging else 'native-asgi')
PYTHON=Path('/home/myexperi/virtualenv')/ROOT.name/'3.12/bin/python'
SUPERVISOR=PYTHON.parent
PUBLIC='https://staging.my-experiment.ir/lifetrade' if staging else 'https://my-experiment.ir/lifetrade'
PORT=8766 if staging else 8767
ROUTING=Path('/home/myexperi/public_html/staging/lifetrade/.htaccess' if staging else '/home/myexperi/public_html/lifetrade/.htaccess')
CTL=[str(SUPERVISOR/'supervisorctl'),'-c',str(NATIVE/'supervisor.ini')]
phase='preflight';switched=False;stopped=False;daemon_started=False;mirrored=False
result={'commit_sha':a.commit,'environment':a.environment,'deployed':False}

def digest(path):return hashlib.sha256(path.read_bytes()).hexdigest()
def run(command,**kwargs):
    p=subprocess.run(command,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=120,**kwargs)
    if p.returncode:raise RuntimeError('Private operation failed; inspect host-side metadata')
    return p

def ctl(action,check=True):
    p=subprocess.run(CTL+[action,'barsan-native'],capture_output=True,timeout=45)
    if check and p.returncode:raise RuntimeError('Supervisor '+action+' failed')
    return p.returncode==0

def health(url):
    with urllib.request.urlopen(url+'/api/health',timeout=8) as response:
        body=json.load(response)
        return response.status==200 and body.get('database_connected') is True and body.get('database')=='postgresql'

def ready():
    consecutive=0
    for _ in range(45):
        try:ok=health('http://127.0.0.1:'+str(PORT))
        except Exception:ok=False
        consecutive=consecutive+1 if ok else 0
        if consecutive==3:return
        time.sleep(1)
    raise RuntimeError('Native readiness failed')

def atomic(path,data,mode=0o600):
    path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    temporary=path.with_name(path.name+'.'+a.commit[:12]+'.new')
    assert not temporary.exists()
    temporary.write_bytes(data);os.chmod(temporary,mode);os.replace(temporary,path)

def private_env(path):
    values={}
    for raw in path.read_text().splitlines():
        if raw.strip() and not raw.lstrip().startswith('#') and '=' in raw:
            key,value=raw.split('=',1);values[key.strip()]=value.strip()
    return values

try:
    assert JOB.is_dir() and not (JOB/'deployment-result.json').exists()
    assert digest(JOB/'runtime-bundle.tar.gz')==a.bundle_sha256
    SOURCE.mkdir(mode=0o700)
    with tarfile.open(JOB/'runtime-bundle.tar.gz') as archive:
        for member in archive.getmembers():
            assert member.isfile() and not member.issym() and not member.islnk()
            assert (SOURCE/member.name).resolve().is_relative_to(SOURCE.resolve())
            archive.extract(member,SOURCE,filter='data')
    manifest=json.loads((SOURCE/'runtime-manifest.json').read_text())
    assert manifest['source_commit']==a.commit
    expected=manifest['files']
    for name,sha in expected.items():assert digest(SOURCE/name)==sha
    (SOURCE/'app/Stimuli').mkdir(exist_ok=True)
    asset_source=ROOT/'repo/app/Stimuli'
    if not staging:
        stage_manifest=json.loads(Path('/home/myexperi/lifetrade-staging/current-release.json').read_text())
        asset_source=Path(stage_manifest['runtime_source'])/'app/Stimuli'
    if asset_source.is_dir():
        assert not asset_source.is_symlink()
        for path in sorted(asset_source.rglob('*')):
            assert not path.is_symlink(), 'Public stimulus symlinks require explicit review'
            if path.is_file():
                relative='app/Stimuli/'+str(path.relative_to(asset_source))
                target=SOURCE/relative;target.parent.mkdir(parents=True,exist_ok=True)
                shutil.copy2(path,target);expected[relative]=digest(target)
    result['runtime_file_count']=len(expected)
    result['runtime_set_sha256']=hashlib.sha256(json.dumps(expected,sort_keys=True).encode()).hexdigest()
    backup=JOB/'backup';backup.mkdir(mode=0o700)
    for name,path in [('routing',ROUTING),('launcher',NATIVE/'launch_native.py'),('release',ROOT/'current-release.json'),('env',ROOT/'config/app.env')]:
        if path.exists():shutil.copy2(path,backup/name)
    before={}
    for name in expected:
        path=ROOT/'repo'/name
        before[name]=digest(path) if path.exists() else None
        if path.exists():
            dest=backup/'repo'/name;dest.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(path,dest)
    (JOB/'source-before.json').write_text(json.dumps(before,indent=2))
    if staging:
        values=private_env(ROOT/'config/app.env')
    else:
        # Preserve production credentials from its existing owning configuration.
        values={}
        for line in ROUTING.read_text().splitlines():
            try:bits=shlex.split(line)
            except ValueError:continue
            if len(bits)==3 and bits[0].lower()=='setenv':values[bits[1]]=bits[2]
        if (ROOT/'config/app.env').exists():values.update(private_env(ROOT/'config/app.env'))
        assert values.get('ADMIN_TOKEN') and values.get('DATABASE_URL')
        values.setdefault('STUDY_CODE','main-study');values.setdefault('STUDY_NAME','BarSan Experiment')
        values['PUBLIC_EXPERIMENT_URL']=PUBLIC
        values.setdefault('INVITATION_TOKEN_KEY_FILE','/home/myexperi/.barsan_invitation_tokens.key')
    info=conninfo_to_dict(values['DATABASE_URL'])
    assert info['dbname']==('myexperi_barsan_staging' if staging else 'myexperi_lifetrade_study')
    assert Path(values['INVITATION_TOKEN_KEY_FILE']).is_file()
    if not staging:
        accepted=json.loads(Path('/home/myexperi/lifetrade-staging/final-acceptance.json').read_text())
        assert accepted['passed'] is True and accepted['runtime_files']==expected
        stage_release=json.loads(Path('/home/myexperi/lifetrade-staging/current-release.json').read_text())
        assert stage_release['runtime_files']==expected
        # Check old Production authentication without exposing any response data.
        request=urllib.request.Request(PUBLIC+'/api/admin/progress?scope=main&include_timing=false',headers={'X-Admin-Token':values['ADMIN_TOKEN']})
        try:
            with urllib.request.urlopen(request,timeout=10) as response:assert response.status==200
        except (TimeoutError, OSError):
            pass  # Known Passenger stalls must not prevent the verified native cutover.
    phase='database-backup'
    pg_env=os.environ.copy()
    for field,key in [('host','PGHOST'),('port','PGPORT'),('dbname','PGDATABASE'),('user','PGUSER'),('password','PGPASSWORD')]:
        if field in info:pg_env[key]=info[field]
    dump=JOB/'database-before.dump'
    run(['pg_dump','-Fc','--no-owner','--no-acl','-f',str(dump)],env=pg_env)
    assert dump.stat().st_size>0
    run(['pg_restore','--list',str(dump)])
    shutil.copy2(values['INVITATION_TOKEN_KEY_FILE'],backup/'invitation.key')
    with psycopg.connect(values['DATABASE_URL']) as connection:
        data_before={table:connection.execute('SELECT COUNT(*) FROM '+table).fetchone()[0]
                     for table in ['participants','study_participations','instrument_runs','lifetrade_trials','ultimatum_trials','questionnaire_responses']}
    phase='runtime-preparation'
    versions=run([str(PYTHON),'-c',"import importlib.metadata,json;print(json.dumps({k:importlib.metadata.version(k) for k in ['fastapi','uvicorn','psycopg','psycopg-pool']}))"])
    required={'fastapi':'0.115.12','uvicorn':'0.34.0','psycopg':'3.2.6','psycopg-pool':'3.2.6'}
    assert json.loads(versions.stdout)==required
    supervisor_check=[str(PYTHON),'-c',"import importlib.metadata;print(importlib.metadata.version('supervisor'))"]
    installed=subprocess.run(supervisor_check,capture_output=True,timeout=15)
    if installed.returncode and not staging:
        run([str(PYTHON),'-m','pip','install','--disable-pip-version-check','--no-deps','supervisor==4.3.0'])
        installed=run(supervisor_check)
    assert installed.returncode==0 and installed.stdout.strip()==b'4.3.0'
    for key,value in {'DB_POOL_MIN_SIZE':'1','DB_POOL_MAX_SIZE':'4','DB_POOL_TIMEOUT':'5','DB_POOL_MAX_WAITING':'128','DB_CONNECT_TIMEOUT':'5'}.items():values[key]=value
    assert '\n' not in ''.join(values.values())
    if not staging:atomic(ROOT/'config/app.env',''.join(key+'='+value+'\n' for key,value in values.items()).encode())
    launcher='''from pathlib import Path
import hashlib,json,os
ROOT=Path(ROOT_VALUE)
SOURCE=Path(SOURCE_VALUE)
PYTHON=PYTHON_VALUE
env=os.environ.copy()
for raw in (ROOT/'config/app.env').read_text().splitlines():
 if raw.strip() and not raw.lstrip().startswith('#') and '=' in raw:
  key,value=raw.split('=',1);env[key.strip()]=value.strip()
assert env.get('PUBLIC_EXPERIMENT_URL','').rstrip('/')==PUBLIC_VALUE
for name,sha in FILES_VALUE.items():
 assert hashlib.sha256((SOURCE/name).read_bytes()).hexdigest()==sha
for key,value in {'BARSAN_SYNC_THREADS':'8','BARSAN_REQUEST_METRICS':'1','DB_POOL_MIN_SIZE':'1','DB_POOL_MAX_SIZE':'4','DB_POOL_TIMEOUT':'5','DB_POOL_MAX_WAITING':'128','DB_CONNECT_TIMEOUT':'5'}.items():env[key]=value
os.chdir(SOURCE)
os.execve(PYTHON,[PYTHON,'-m','uvicorn','barsan_experiment:app','--host','127.0.0.1','--port',PORT_VALUE,'--workers','2','--root-path','/lifetrade','--proxy-headers','--forwarded-allow-ips','127.0.0.1','--no-access-log','--header','X-Barsan-Runtime:uvicorn-final-native'],env)
'''
    for key,value in {'ROOT_VALUE':repr(str(ROOT)),'SOURCE_VALUE':repr(str(SOURCE)),'PYTHON_VALUE':repr(str(PYTHON)),
                      'PUBLIC_VALUE':repr(PUBLIC),'FILES_VALUE':repr(expected),'PORT_VALUE':repr(str(PORT))}.items():launcher=launcher.replace(key,value)
    compile(launcher,'launch_native.py','exec')
    if staging:
        ctl('stop');stopped=True
    else:NATIVE.mkdir(mode=0o700)
    atomic(NATIVE/'launch_native.py',launcher.encode())
    # Warm up additive schema once before starting two workers concurrently.
    runtime_env=os.environ.copy();runtime_env.update(values);runtime_env['DB_POOL_MAX_SIZE']='4'
    run([str(PYTHON),'-c','import barsan_experiment'],cwd=SOURCE,env=runtime_env)
    phase='native-start'
    if staging:ctl('start')
    else:
        cfg=f'''[unix_http_server]
file={NATIVE}/supervisor.sock
chmod=0600
[supervisord]
logfile={NATIVE}/supervisor.log
logfile_maxbytes=5MB
logfile_backups=2
pidfile={NATIVE}/supervisord.pid
childlogdir={NATIVE}
umask=0077
[rpcinterface:supervisor]
supervisor.rpcinterface_factory=supervisor.rpcinterface:make_main_rpcinterface
[supervisorctl]
serverurl=unix://{NATIVE}/supervisor.sock
[program:barsan-native]
command={PYTHON} {NATIVE}/launch_native.py
directory={SOURCE}
autostart=true
autorestart=true
startsecs=5
startretries=3
stopsignal=TERM
stopasgroup=true
killasgroup=true
stopwaitsecs=15
redirect_stderr=true
stdout_logfile={NATIVE}/uvicorn-supervised.log
stdout_logfile_maxbytes=20MB
stdout_logfile_backups=2
'''
        atomic(NATIVE/'supervisor.ini',cfg.encode())
        run([str(SUPERVISOR/'supervisord'),'-c',str(NATIVE/'supervisor.ini')]);daemon_started=True
    ready()
    phase='routing'
    if not staging:
        original=ROUTING.read_text()
        proxy='''# BarSan native ASGI release; previous Passenger config retained below.
RewriteEngine On
RewriteRule ^(.*)$ http://127.0.0.1:8767/$1 [P,L,E=Proxy-Host:my-experiment.ir]
'''
        atomic(ROUTING,(proxy+original).encode(),0o644);switched=True
    phase='public-verification'
    for _ in range(3):assert health(PUBLIC)
    with urllib.request.urlopen(PUBLIC+'/',timeout=20) as response:
        html=response.read().decode();assert 'barsan_clock_journal_v2_' in html and 'recoveryIsHeld' in html
    with urllib.request.urlopen(PUBLIC+'/assets/vazirmatn-v33.003.woff2',timeout=15) as response:
        assert hashlib.sha256(response.read()).hexdigest()==expected['app/fonts/vazirmatn-v33.003.woff2']
    public_assets=[name for name in expected if name.startswith('app/Stimuli/') and name.lower().endswith(('.png','.jpg','.webp','.gif'))]
    if public_assets:
        asset=public_assets[0]
        with urllib.request.urlopen(PUBLIC+'/Stimuli/'+urllib.parse.quote(asset[len('app/Stimuli/'):]),timeout=15) as response:
            assert hashlib.sha256(response.read()).hexdigest()==expected[asset]
    result['legacy_public_assets_verified']=bool(public_assets)
    request=urllib.request.Request(PUBLIC+'/api/admin/progress?scope=main&include_timing=false',headers={'X-Admin-Token':values['ADMIN_TOKEN']})
    with urllib.request.urlopen(request,timeout=20) as response:assert response.status==200
    mirrored=True
    for name in expected:
        if not (ROOT/'repo'/name).is_file() or digest(ROOT/'repo'/name)!=expected[name]:
            atomic(ROOT/'repo'/name,(SOURCE/name).read_bytes(),0o644)
    release={'commit_sha':a.commit,'runtime_files':expected,'runtime_source':str(SOURCE),
             'runtime_mode':'uvicorn-via-litespeed-loopback-proxy','deployed_at':datetime.now(timezone.utc).isoformat(),
             'workers':2,'sync_threads_per_worker':8,'db_pool_max_per_worker':4,'loopback_port':PORT}
    atomic(ROOT/'current-release.json',(json.dumps(release,indent=2)+'\n').encode())
    if not staging:
        existing=subprocess.run(['crontab','-l'],capture_output=True,text=True,timeout=10)
        assert existing.returncode in (0,1)
        cron=existing.stdout if existing.returncode==0 else ''
        reboot='@reboot '+str(SUPERVISOR/'supervisord')+' -c '+str(NATIVE/'supervisor.ini')
        if reboot not in cron:run(['crontab','-'],input=(cron.rstrip()+'\n'+reboot+'\n').lstrip().encode())
    with psycopg.connect(values['DATABASE_URL']) as connection:
        data_after={table:connection.execute('SELECT COUNT(*) FROM '+table).fetchone()[0] for table in data_before}
    assert all(data_after[table]>=data_before[table] for table in data_before)
    result.update(deployed=True,public_health=True,html_verified=True,font_verified=True,admin_verified=True,
        backup_verified=True,backup_bytes=dump.stat().st_size,data_counts_before=data_before,data_counts_after=data_after,
        runtime_files=expected,runtime_source=str(SOURCE),independent_database=True)
except Exception as error:
    result.update(failed_phase=phase,error_type=type(error).__name__)
    try:
        if not staging and switched:atomic(ROUTING,(backup/'routing').read_bytes(),0o644)
        if staging and stopped:
            ctl('stop',check=False);atomic(NATIVE/'launch_native.py',(backup/'launcher').read_bytes());ctl('start');ready()
        elif not staging and daemon_started:ctl('stop',check=False)
        if mirrored:
            for name,sha in before.items():
                if sha:atomic(ROOT/'repo'/name,(backup/'repo'/name).read_bytes(),0o644)
        if (backup/'release').exists():atomic(ROOT/'current-release.json',(backup/'release').read_bytes())
        result['rollback_health']=health(PUBLIC)
    except Exception as rollback_error:result['rollback_error_type']=type(rollback_error).__name__
    (JOB/'deployment-result.json').write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps({key:value for key,value in result.items() if key!="runtime_files"}));raise SystemExit(2)
(JOB/'deployment-result.json').write_text(json.dumps(result,indent=2)+'\n')
print(json.dumps({key:value for key,value in result.items() if key!="runtime_files"}))
