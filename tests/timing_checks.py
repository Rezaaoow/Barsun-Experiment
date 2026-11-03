import uuid
from concurrent.futures import ThreadPoolExecutor
from resilience_checks import setup_module_run
import barsan_experiment as main

def check_client_timing(client):
    identity, started, step = setup_module_run(client, 'ultimatum')
    row = dict(run_id=started['run_id'],clock_id=str(uuid.uuid4()),step_id=step,stage_code='ultimatum',
               sequence=1,visible_ms=90000,uncertain_ms=0,closed=False)
    def send(rows):
        return client.post('/api/experiment/time/checkpoints',json={**identity,'checkpoints':rows})
    with ThreadPoolExecutor(max_workers=6) as pool:
        responses=list(pool.map(lambda _:send([row]),range(6)))
    assert all(r.status_code==200 for r in responses)
    assert send([{**row,'visible_ms':89999}]).status_code==409
    last={**row,'sequence':3,'visible_ms':150000,'uncertain_ms':60000,'closed':True}
    assert send([last]).status_code==200
    assert send([{**row,'sequence':2,'visible_ms':120000}]).status_code==200
    assert send([{**last,'sequence':4,'visible_ms':150001}]).status_code==409
    assert send([{**row,'clock_id':'other-clock'}]).status_code==409
    assert send([{**row,'run_id':'not-owned'}]).status_code==403
    assert send([{**row,'stage_code':'dd_lifetrade'}]).status_code==422
    assert send([{**row,'uncertain_ms':90001}]).status_code==422
    # Atomic batch: a following invalid run cannot leave a partial clock behind.
    other_identity,other_started,other_step=setup_module_run(client,'dd_lifetrade')
    other=dict(row,run_id=other_started['run_id'],step_id=other_step,stage_code='dd_lifetrade')
    bad=client.post('/api/experiment/time/checkpoints',json={**other_identity,'checkpoints':[other,{**other,'run_id':'zz-missing'}]})
    assert bad.status_code==403
    marker='%s' if main.DATABASE_URL else '?'
    with main.conn() as connection:
        saved=connection.execute('SELECT * FROM experiment_client_clocks WHERE run_id='+marker,(row['run_id'],)).fetchone()
        assert saved['visible_ms']==150000 and saved['uncertain_ms']==60000 and saved['sequence']==3
        assert connection.execute('SELECT COUNT(*) AS n FROM experiment_client_clock_checkpoints WHERE run_id='+marker,(row['run_id'],)).fetchone()['n']==3
        assert connection.execute('SELECT COUNT(*) AS n FROM experiment_client_clocks WHERE run_id='+marker,(other['run_id'],)).fetchone()['n']==0
    # Superseded attempts may upload their OWN late timing without contaminating a new attempt.
    restarted=client.post('/api/experiment/stages/start',json={**identity,'stage_code':'ultimatum','step_id':step,'start_request_id':str(uuid.uuid4())})
    assert restarted.status_code==200 and restarted.json()['run_id']!=row['run_id']
    assert send([last]).status_code==200
    summary=client.get('/api/admin/progress?scope=test&include_timing=true',headers={'X-Admin-Token':main.ADMIN_TOKEN})
    assert summary.status_code==200
    matching=[step for item in summary.json() for step in item['workflow_steps'] if step['step_id']==row['step_id'] and step['active_seconds']==150]
    assert matching and matching[0]['time_source']=='client_visible_v2' and matching[0]['uncertain_seconds']==60
    exported=client.get('/api/admin/time/checkpoints.csv?scope=test',headers={'X-Admin-Token':main.ADMIN_TOKEN})
    assert exported.status_code==200 and row['run_id'] in exported.text
    assert client.get('/api/admin/time/checkpoints.csv?scope=test').status_code==401
    return row

