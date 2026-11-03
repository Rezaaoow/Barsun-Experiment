"""Temporarily use only the same two previously authorized synthetic Staging accounts."""
from pathlib import Path
import json
import os
import sys
import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg.conninfo import conninfo_to_dict
os.umask(0o077)
commit = sys.argv[1]
root = Path('/home/myexperi/lifetrade-staging')
assert json.loads((root/'current-release.json').read_text())['commit_sha'] == commit
job = root/'deployments'/('modules-'+commit[:12])
snapshot = job/'two-synthetic-accounts-before.json'
assert not snapshot.exists()
groups = json.loads((root/'deployments/final-browser-20261103/groups-private.json').read_text())
assert len(groups) == 2 and all(g['study_code'].startswith('barsan-test-group-') for g in groups)
values = {}
for line in (root/'config/app.env').read_text().splitlines():
    if line.strip() and not line.lstrip().startswith('#') and '=' in line:
        key,value=line.split('=',1);values[key.strip()]=value.strip()
assert conninfo_to_dict(values['DATABASE_URL'])['dbname'] == 'myexperi_barsan_staging'
with psycopg.connect(values['DATABASE_URL'], row_factory=dict_row) as connection:
    assignments = connection.execute('SELECT a.assignment_id,a.study_code,a.group_link_id,a.workflow_json,a.active,p.participant_code,sp.participation_id FROM experiment_assignments a JOIN participants p USING(participant_id) JOIN study_participations sp ON sp.participant_id=a.participant_id AND sp.study_code=a.study_code WHERE a.study_code=ANY(%s) AND a.group_link_id=ANY(%s) FOR UPDATE OF a',([g['study_code'] for g in groups],[g['group_link_id'] for g in groups])).fetchall()
    assert len(assignments) == 2 and all(a['participant_code']=='FINAL-UI-20261103' for a in assignments)
    stages = connection.execute('SELECT * FROM experiment_stage_progress WHERE participation_id=ANY(%s)',([a['participation_id'] for a in assignments],)).fetchall()
    snapshot.write_text(json.dumps({'assignments':assignments,'legacy_stages':stages},default=str,indent=2))
    steps=[{'step_id':'modules-dd-one-'+commit[:8],'module_code':'dd_standalone'},
           {'step_id':'modules-rest-'+commit[:8],'module_code':'intermission'},
           {'step_id':'modules-dd-two-'+commit[:8],'module_code':'dd_standalone'}]
    connection.execute('UPDATE experiment_assignments SET workflow_json=%s,active=1 WHERE assignment_id=ANY(%s)',(json.dumps(steps),[a['assignment_id'] for a in assignments]))
print(json.dumps({'same_previously_approved_accounts':2,'raw_rows_deleted':0,'real_accounts_affected':0,'snapshot_saved':True}))
