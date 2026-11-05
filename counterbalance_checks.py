"""Shared disposable SQLite/PostgreSQL counterbalance concurrency checks."""
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse,parse_qs


def check_atomic_counterbalancing(main,client):
    headers={'X-Admin-Token':main.ADMIN_TOKEN}
    for variant in ('frame','control'):
        group=client.post('/api/admin/group-links',headers=headers,json={'scope':'test','label':'Synthetic counterbalance concurrency','steps':[{'step_id':'cb-ut','module_code':'ultimatum_'+variant}]}).json()
        token=parse_qs(urlparse(group['invitation_url']).query)['group'][0];payloads=[]
        for i in range(24):
            claim=client.post('/api/experiment/group/claim',json={'group_token':token,'participant_code':'SYN-CB-'+uuid.uuid4().hex,'first_name':'Synthetic','last_name':'Counterbalance','claim_request_id':str(uuid.uuid4())})
            assert claim.status_code==200
            identity={key:claim.json()[key] for key in ('participant_code','invitation_token')}
            assert client.post('/api/experiment/consent',json={**identity,'first_name':'Synthetic','last_name':'Counterbalance','accepted':True}).status_code==200
            payloads.append({**identity,'stage_code':'ultimatum','step_id':'cb-ut','start_request_id':str(uuid.uuid4())})
        with ThreadPoolExecutor(max_workers=12) as executor:
            replies=list(executor.map(lambda body:client.post('/api/experiment/stages/start',json=body),payloads+payloads[:12]))
        assert all(r.status_code==200 for r in replies)
        data=[r.json() for r in replies];plans=[r['draft_payload']['randomization'] for r in data[:24]]
        assert len({r['run_id'] for r in data})==24
        assert len({tuple(p['block_order']) for p in plans})==24
        assert {p['ticket'] for p in plans}==set(range(24))
        assert {p['stratum'] for p in plans}=={variant.title()}
        for p in plans:
            retry=client.post('/api/experiment/stages/start',json=payloads[plans.index(p)]).json()
            assert retry['draft_payload']['randomization']==p
        assert all(Counter(p['block_order'][position] for p in plans)=={block:6 for block in main.ULTIMATUM_BLOCKS} for position in range(4))
