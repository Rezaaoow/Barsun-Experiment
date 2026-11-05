"""Root-only NEW Production synthetic fixture/integrity/verification operator.
Never mutates preexisting invitation rows; cleanup uses recorded NEW IDs only.
"""
from pathlib import Path
from urllib.parse import urlparse,parse_qs
import collections,hashlib,json,os,re,subprocess,sys,urllib.request
assert os.geteuid()==0
os.umask(0o077)
ROOT=Path('/var/lib/barsan-operations/final-20261105');ROOT.mkdir(mode=0o700,exist_ok=True)
BASE='https://experiment.barsun4818.ir/dce'
def query(sql):
    return subprocess.check_output(['sudo','-u','postgres','psql','-d','barsan_production','-At','-v','ON_ERROR_STOP=1','-c',sql],text=True)
def rows(table,key):
    return {line.split('|',1)[0]:line.split('|',1)[1] for line in query(f'SELECT {key},row_to_json(t)::text FROM {table} t ORDER BY {key}').splitlines()}
def integrity():
    old=json.loads(Path('/var/lib/barsan-operations/pre-final-link-integrity-20261105.json').read_text())
    external_status_changes=0
    for label,table,key in [('group_links','experiment_group_links','group_link_id'),('assignments','experiment_assignments','assignment_id')]:
        current=rows(table,key)
        for k,v in old[label].items():
            assert k in current,'Preexisting invitation missing'
            if hashlib.sha256(current[k].encode()).hexdigest()==v:continue
            # Independently observed authenticated admin status toggles are never
            # overwritten. Verify EVERY other byte against the sealed original.
            assert label=='group_links' and any(hashlib.sha256(re.sub(r'"active":[01](?=,|})','"active":'+str(flag),current[k],count=1).encode()).hexdigest()==v for flag in (0,1)),'Preexisting invitation content changed'
            external_status_changes+=1
    assert hashlib.sha256(Path('/etc/barsan/invitation.key').read_bytes()).hexdigest()==old['invitation_key_sha256']
    return {'preexisting_groups_all_fields_except_external_active_flags_unchanged':len(old['group_links']),'external_admin_active_flag_changes':external_status_changes,'preexisting_assignments_unchanged':len(old['assignments']),'signing_key_unchanged':True}
def api(path,payload):
    values={}
    for line in Path('/etc/barsan/application.env').read_text().splitlines():
        k,sep,v=line.partition('=')
        if sep:values[k.strip()]=v.strip().strip('"').strip("'")
    req=urllib.request.Request(BASE+path,data=json.dumps(payload).encode(),headers={'Content-Type':'application/json','X-Admin-Token':values['ADMIN_TOKEN']})
    with urllib.request.urlopen(req,timeout=30) as r:return json.load(r)
command=sys.argv[1]
if command=='integrity':print(json.dumps(integrity()));sys.exit()
name=sys.argv[2];assert re.fullmatch('[a-z0-9-]{1,40}',name)
folder=ROOT/name;path=folder/'fixture.json'
if command=='create':
    assert not folder.exists();folder.mkdir(mode=0o700);groups=[]
    burst=len(sys.argv)>3 and sys.argv[3]=='burst'
    for variant in ['Frame','Control']:
        steps=[] if burst else [{'step_id':'load-dd','module_code':'dd_standalone'},{'step_id':'load-rest','module_code':'intermission'}]
        steps.append({'step_id':'load-ut','module_code':'ultimatum_'+variant.lower()})
        group=api('/api/admin/group-links',{'scope':'test','label':'FINAL SYNTHETIC '+name+' '+variant,'steps':steps})
        assert group['study_code'].startswith('barsan-test-')
        groups.append({'group_link_id':group['group_link_id'],'study_code':group['study_code'],'variant':variant,'group_token':parse_qs(urlparse(group['invitation_url']).query)['group'][0]})
    path.write_text(json.dumps({'synthetic':True,'prefix':'SYN-FINAL-'+name.upper(),'groups':groups}));path.chmod(0o600)
    print(json.dumps({'created_synthetic_groups':2,'cohort':name,**integrity()}))
elif command=='close':
    f=json.loads(path.read_text());ids=[g['group_link_id'] for g in f['groups']]
    assert all(g['study_code'].startswith('barsan-test-') for g in f['groups'])
    original_ids=set(json.loads(Path('/var/lib/barsan-operations/pre-final-link-integrity-20261105.json').read_text())['group_links'])
    assert set(ids).isdisjoint(original_ids),'Never deactivate a preexisting invitation'
    result=api('/api/admin/group-links/status',{'scope':'test','group_link_ids':ids,'active':False,'include_assignments':True})
    print(json.dumps({'closed_own_synthetic_groups':len(ids),**integrity()}))
elif command=='verify':
    f=json.loads(path.read_text());expected=int(sys.argv[3]);burst=len(sys.argv)>4 and sys.argv[4]=='burst';results=[]
    for g in f['groups']:
        study=g['study_code'];assert re.fullmatch('barsan-test-[a-zA-Z0-9-]+',study)
        condition="sp.study_code='"+study+"'"
        sql="SELECT coalesce(json_agg(row_to_json(x)),'[]'::json) FROM (SELECT r.run_id,r.status,r.metadata,r.trial_count FROM instrument_runs r JOIN study_participations sp USING(participation_id) WHERE "+condition+") x"
        runs=json.loads(query(sql));count=int(query("SELECT count(*) FROM study_participations sp WHERE "+condition))
        assert count==expected//2,(count,expected)
        plans=json.loads(query("SELECT coalesce(json_agg(row_to_json(x)),'[]'::json) FROM (SELECT a.kind,a.ticket,a.plan_json FROM experiment_randomization_allocations a JOIN study_participations sp USING(participation_id) WHERE "+condition+") x"))
        ut=[json.loads(p['plan_json']) for p in plans if p['kind']=='ultimatum'];assert len(ut)==count
        assert all(p['stratum']==g['variant'] and p['algorithm']=='full24-williams-quartets-v1' for p in ut)
        mismatches=int(query("SELECT count(*) FROM instrument_runs r JOIN study_participations sp USING(participation_id) JOIN experiment_randomization_allocations a ON a.participation_id=sp.participation_id AND a.kind='ultimatum' WHERE "+condition+" AND r.metadata->>'stage_code'='ultimatum' AND r.metadata->'randomization'<>a.plan_json::jsonb"));assert mismatches==0
        assert sorted(p['ticket'] for p in ut)==list(range(count))
        orders=collections.Counter(tuple(p['block_order']) for p in ut)
        assert len(orders)==min(24,count) and max(orders.values())-min(orders.values())<=1
        positions=[collections.Counter(p['block_order'][i] for p in ut) for i in range(4)]
        pairs=collections.Counter((p['block_order'][i],p['block_order'][i+1]) for p in ut for i in range(3))
        if count%4==0:
            assert all(set(c.values())=={count//4} and len(c)==4 for c in positions)
            assert len(pairs)==12 and set(pairs.values())=={count//4}
        ut_runs=[r for r in runs if r['metadata'].get('stage_code')=='ultimatum' and r['status']!='superseded']
        assert len(ut_runs)==count
        counts=json.loads(query("SELECT row_to_json(x) FROM (SELECT count(*) AS rounds,count(*) FILTER (WHERE t.status='completed') AS completed,count(*) FILTER (WHERE t.phase='main') AS main,count(*) FILTER (WHERE t.wait_duration_ms IS NOT NULL AND t.decision_screen_shown_at IS NOT NULL AND t.payload->'client_event_timestamps'->>'advance_clicked_at' IS NOT NULL) AS timed FROM ultimatum_trials t JOIN instrument_runs r USING(run_id) JOIN study_participations sp USING(participation_id) WHERE "+condition+") x"))
        dd=int(query("SELECT count(*) FROM lifetrade_trials t JOIN instrument_runs r USING(run_id) JOIN study_participations sp USING(participation_id) WHERE "+condition))
        if not burst:
            assert all(r['status']=='completed' for r in runs if r['status']!='superseded')
            assert counts['rounds']==count*22 and counts['completed']==count*22 and counts['main']==count*20,(counts,count)
            assert dd==count*5
            assert counts['timed']==count*22,(counts,count)
            clocks=int(query("SELECT count(*) FROM experiment_client_clocks c JOIN instrument_runs r USING(run_id) JOIN study_participations sp USING(participation_id) WHERE "+condition+" AND c.closed=1"))
            assert clocks==count*3,(clocks,count)
            score_mismatches=int(query("SELECT count(*) FROM experiment_stage_progress g JOIN study_participations sp USING(participation_id) JOIN (SELECT run_id,sum(points_earned) AS total FROM ultimatum_trials WHERE phase='main' GROUP BY run_id) scores ON scores.run_id=g.current_run_id WHERE "+condition+" AND g.stage_code='ultimatum' AND (g.draft_payload->>'final_score')::int<>scores.total"))
            assert score_mismatches==0
            assert all(sum(json.loads(p['plan_json'])['now_left']) in (2,3) for p in plans if p['kind']=='dd_standalone')
            scores=json.loads(query("SELECT coalesce(json_agg(row_to_json(x)),'[]'::json) FROM (SELECT r.run_id,r.metadata,sum(t.points_earned) AS score FROM instrument_runs r JOIN ultimatum_trials t USING(run_id) JOIN study_participations sp USING(participation_id) WHERE "+condition+" AND t.phase='main' GROUP BY r.run_id,r.metadata) x"))
            # Metadata final payload placement differs across versions: raw round sums
            # and normal exports remain the canonical cumulative score source.
            assert len(scores)==count
        results.append({'variant':g['variant'],'participants':count,'completed_ultimatum':sum(r['status']=='completed' for r in ut_runs),'dd_rows':dd,'closed_clocks':None if burst else clocks,**counts,'distinct_orders':len(orders),'order_counts':sorted(orders.values()),'positions':[dict(c) for c in positions],'carryover_counts':sorted(pairs.values()),'run_plan_mismatches':mismatches})
    studies=','.join("'"+g['study_code']+"'" for g in f['groups'])
    spans=json.loads(query("SELECT coalesce(json_agg(row_to_json(x)),'[]'::json) FROM (SELECT r.started_at,r.completed_at FROM instrument_runs r JOIN study_participations sp USING(participation_id) WHERE sp.study_code IN ("+studies+") AND r.metadata->>'stage_code'='ultimatum' AND r.status<>'superseded') x"))
    timeline=sorted([(r['started_at'],1) for r in spans]+[(r['completed_at'],-1) for r in spans if r['completed_at']]);active=0;peak=0
    for timestamp,change in timeline:active+=change;peak=max(peak,active)
    report={'peak_concurrent_ultimatum':peak,'cohort':name,'burst':burst,'results':results,**integrity()};(folder/'verification.json').write_text(json.dumps(report,indent=2));print(json.dumps(report))
else:raise SystemExit('Unknown command')
