"""Scope isolation must survive completely filtered keyset pages."""
import csv,io,uuid
import barsan_experiment as main
from resilience_checks import setup_module_run


def check_filtered_export_pages(client):
    first=setup_module_run(client,'dd_lifetrade')
    second=setup_module_run(client,'dd_lifetrade')
    foreign,target=sorted([first,second],key=lambda x:x[1]['run_id'])
    for identity,started,step,count,prefix in [(*foreign,201,'000-foreign-'),(*target,1,'zzz-target-')]:
        for offset in range(0,count,25):
            trials=[{'client_event_id':prefix+str(i).zfill(4)+'-'+started['run_id'],'row':{'index':i,'task':'LT','research_marker':prefix}}
                    for i in range(offset,min(offset+25,count))]
            response=client.post('/api/trials',json={'session_id':started['run_id'],'trials':trials})
            assert response.status_code==200
        for offset in range(0,count,25):
            rows=[dict(run_id=started['run_id'],clock_id='clock-'+started['run_id'],step_id=step,stage_code='dd_lifetrade',
                       sequence=i+1,visible_ms=i+1,uncertain_ms=0,closed=False) for i in range(offset,min(offset+25,count))]
            response=client.post('/api/experiment/time/checkpoints',json={**identity,'checkpoints':rows})
            assert response.status_code==200
    study=client.post('/api/experiment/identify',json=target[0]).json()['study_code']
    old_study=main.STUDY_CODE
    try:
        main.STUDY_CODE=study
        for path in ['/api/admin/dd/records.csv','/api/admin/time/checkpoints.csv']:
            response=client.get(path+'?scope=main',headers={'X-Admin-Token':main.ADMIN_TOKEN})
            assert response.status_code==200
            rows=list(csv.DictReader(io.StringIO(response.text)))
            assert len(rows)==1 and rows[0]['run_id']==target[1]['run_id']
            assert foreign[1]['run_id'] not in response.text
            test=client.get(path+'?scope=test',headers={'X-Admin-Token':main.ADMIN_TOKEN})
            assert test.status_code==200
            filtered=[row for row in csv.DictReader(io.StringIO(test.text)) if row['run_id'] in [target[1]['run_id'],foreign[1]['run_id']]]
            assert len(filtered)==202
    finally:
        main.STUDY_CODE=old_study
