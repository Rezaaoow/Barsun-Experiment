"""Isolated Staging database check. Never target the real study or Production."""
import os
from pathlib import Path
import sys
import json
import uuid
from psycopg.conninfo import conninfo_to_dict

os.umask(0o077)
root=Path('/home/myexperi/lifetrade-staging')
release=json.loads((root/'current-release.json').read_text())
expected=sys.argv[1]
assert release['commit_sha']==expected and len(expected)==40
values={}
for line in (root/'config/app.env').read_text().splitlines():
    if line.strip() and not line.lstrip().startswith('#') and '=' in line:
        key,value=line.split('=',1);values[key.strip()]=value.strip()
assert conninfo_to_dict(values['DATABASE_URL'])['dbname']=='myexperi_barsan_staging'
os.environ.update(values)
os.environ['STUDY_CODE']='barsan-test-admin-repair-'+uuid.uuid4().hex
os.environ['STUDY_NAME']='Synthetic isolated admin repair'
sys.path.insert(0,release['runtime_source'])
from fastapi.testclient import TestClient
import barsan_experiment as main
from admin_maintenance_checks import check_admin_maintenance
assert main.STUDY_CODE==os.environ['STUDY_CODE']
with TestClient(main.app) as client:
    result=check_admin_maintenance(client,disposable_database=False)
result.update(source_commit=expected,live_staging=True,isolated_synthetic_study=True,real_study_reset=False)
(root/'deployments'/('modules-'+expected[:12])/'admin-acceptance.json').write_text(json.dumps(result,indent=2))
print(json.dumps(result))
