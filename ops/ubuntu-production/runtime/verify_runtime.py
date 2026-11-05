"""Root-only, non-destructive runtime verification for the NEW Production host."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess

def main():
    p=argparse.ArgumentParser()
    p.add_argument('--venv',type=Path,default=Path('/opt/barsan/venvs/runtime-20261105-v1'))
    p.add_argument('--wheel-cache',type=Path,default=Path('/var/cache/barsan/wheels-runtime-20261105-v1'))
    p.add_argument('--expect-empty',action='store_true',help='Preparation gate only; omit after app schema/data installation')
    args=p.parse_args()
    assert os.geteuid()==0, 'Run with sudo on the new server'
    source=Path(__file__).resolve().parent
    versions=json.loads((source/'runtime-versions.json').read_text())
    assert (source/'postgresql-local.conf').read_bytes()==Path('/etc/postgresql/16/main/conf.d/90-barsan-local.conf').read_bytes()
    assert (source/'pg_hba.conf').read_bytes()==Path('/etc/postgresql/16/main/pg_hba.conf').read_bytes()
    def run(command):
        result=subprocess.run(command,capture_output=True,text=True,timeout=30)
        assert result.returncode==0, 'Runtime verification command failed: '+Path(command[0]).name
        return result.stdout.strip()
    python=str(args.venv/'bin/python')
    packages=json.loads(run([python,'-c',"import importlib.metadata as m,json;print(json.dumps({d.metadata['Name'].lower().replace('_','-'):d.version for d in m.distributions() if d.metadata['Name'].lower() not in ['pip','setuptools','wheel']}))"]))
    assert packages==versions
    run([python,'-m','pip','--isolated','--disable-pip-version-check','check'])
    lock=(source/'requirements-ubuntu-py312-amd64.lock').read_text()
    for name,version in versions.items():
        matches=list(args.wheel_cache.glob(name.replace('-','_')+'-'+version+'-*.whl'))
        assert len(matches)==1
        digest=hashlib.sha256(matches[0].read_bytes()).hexdigest()
        assert name+'=='+version+' --hash=sha256:'+digest in lock.splitlines()
    env=Path('/etc/barsan/database.env')
    assert env.stat().st_uid==0 and env.stat().st_mode & 0o777==0o640
    assert env.read_text()=='DATABASE_URL=postgresql:///barsan_production?user=barsan-app&host=/var/run/postgresql\n'
    smoke="""from pathlib import Path
import json,psycopg
import fastapi,uvicorn,psycopg_pool,cryptography,a2wsgi
url=Path('/etc/barsan/database.env').read_text().strip().split('=',1)[1]
with psycopg.connect(url) as connection:
 connection.read_only=True
 with connection.cursor() as cursor:
  cursor.execute('SELECT current_database(),current_user');assert cursor.fetchone()==('barsan_production','barsan-app')
  cursor.execute('SELECT rolsuper,rolcreatedb,rolcreaterole,rolreplication FROM pg_roles WHERE rolname=current_user');assert cursor.fetchone()==(False,False,False,False)
  cursor.execute("SELECT count(*) FROM information_schema.tables WHERE table_schema='public' AND table_type='BASE TABLE'");tables=cursor.fetchone()[0]
  cursor.execute('SHOW listen_addresses');assert cursor.fetchone()[0]==''
print(json.dumps({'local_identity_verified':True,'public_tables':tables}))
"""
    result=json.loads(run(['runuser','-u','barsan-app','--',python,'-c',smoke]))
    if args.expect_empty:
        assert result['public_tables']==0
    denied=subprocess.run(['runuser','-u','barsan-deploy','--','psql','postgresql:///barsan_production?user=barsan-app&host=/var/run/postgresql','-XAt','-c','SELECT 1'],capture_output=True,text=True,timeout=15)
    assert denied.returncode!=0 and 'peer authentication failed' in denied.stderr.lower()
    assert subprocess.run(['runuser','-u','barsan-app','--','sudo','-n','true'],capture_output=True).returncode!=0
    assert not any(':5432 ' in line for line in run(['ss','-lnt']).splitlines())
    for service in ['ssh','nginx','postgresql@16-main','certbot.timer','ufw']:
        assert run(['systemctl','is-active',service])=='active'
    print(json.dumps({'runtime_packages_verified':len(packages),'recovery_wheel_hashes_verified':True,'source_configurations_match':True,'local_database_identity_verified':True,'repository_database_impersonation_denied':True,'database_tcp_listener':False,'production_database_empty':result['public_tables']==0,'application_deployed_by_this_check':False}))

if __name__=='__main__':
    main()
