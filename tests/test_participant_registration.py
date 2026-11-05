"""Scoped registration/link-state regression; disposable synthetic SQLite data only."""
import csv
import io
import time
import uuid
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from test_admin_links import client, create_main_assignment, create_group_link, invite_token
from test_browser_resilience import browser_env
import barsan_experiment as main

PROFILE = {'first_name': 'نام آزمایشی', 'last_name': 'خانوادگی آزمایشی'}


def test_names_required_for_first_consent_and_preserved_on_legacy_replay(client):
    item = create_main_assignment(client, code='PROFILE-1')
    identity = {'invitation_token': invite_token(item), 'participant_code': 'PROFILE-1'}
    for invalid in [{}, {'first_name':' '}, {'first_name':'Name','last_name':' '}, {'first_name':'x'*101,'last_name':'Last'}]:
        assert client.post('/api/experiment/consent', json={**identity,'accepted':True,**invalid}).status_code == 422
    accepted = client.post('/api/experiment/consent', json={**identity,'accepted':True,**PROFILE})
    assert accepted.status_code == 200
    assert client.post('/api/experiment/consent',json={**identity,'accepted':True}).json() == accepted.json()
    person = client.post('/api/experiment/identify',json=identity).json()
    assert {key:person[key] for key in PROFILE} == PROFILE
    exported = client.get('/api/admin/assignments/export.csv',headers={'X-Admin-Token':main.ADMIN_TOKEN})
    row = list(csv.DictReader(io.StringIO(exported.text.lstrip('\ufeff'))))[0]
    assert {key:row[key] for key in PROFILE} == PROFILE


def test_group_accepts_code_without_individual_link_and_retains_identity_on_reentry(client):
    group = client.post('/api/admin/group-links',headers={'X-Admin-Token':main.ADMIN_TOKEN},json={
        'scope':'main','label':'Synthetic named group','condition_code':'B',
        'steps':[{'step_id':'dd','module_code':'dd_standalone'}]}).json()
    token = parse_qs(urlparse(group['invitation_url']).query)['group'][0]
    body = {'group_token':token,'participant_code':'PROFILE-GROUP','claim_request_id':str(uuid.uuid4()),**PROFILE}
    for missing in ('first_name','last_name','participant_code'):
        assert client.post('/api/experiment/group/claim',json={k:v for k,v in body.items() if k!=missing}).status_code == 422
    assert client.post('/api/experiment/group/claim',json={**body,'assign_random':True}).status_code == 422
    first = client.post('/api/experiment/group/claim',json=body)
    assert first.status_code == 200
    again = client.post('/api/experiment/group/claim',json=body).json()
    assert again['reused'] and again['invitation_token'] == first.json()['invitation_token']
    identity = {key:again[key] for key in ('participant_code','invitation_token')}
    assert client.post('/api/experiment/consent',json={**identity,**PROFILE,'accepted':True,'group_token':token}).status_code == 200
    person = client.post('/api/experiment/identify',json=identity).json()
    start = {**identity,'stage_code':'dd_lifetrade','step_id':'dd','start_request_id':str(uuid.uuid4())}
    run = client.post('/api/experiment/stages/start',json=start).json()
    assert client.post('/api/experiment/stages/start',json=start).json()['run_id'] == run['run_id']
    refreshed = client.post('/api/experiment/identify',json=identity).json()
    assert refreshed['assignment_id'] == person['assignment_id'] and refreshed['condition_code'] == person['condition_code']
    assert refreshed['workflow_steps'][0]['current_run_id'] == run['run_id']
    assert {key:refreshed[key] for key in PROFILE} == PROFILE
    # Re-entering a new attempt retains participant identity, never copies answers.
    resumed = client.post('/api/experiment/stages/start',json={**start,'start_request_id':str(uuid.uuid4())}).json()
    assert resumed['run_id'] != run['run_id']
    with main.conn() as connection:
        assert connection.execute('SELECT COUNT(*) AS n FROM participants').fetchone()['n'] == 1
        assert connection.execute('SELECT status FROM instrument_runs WHERE run_id=?',(run['run_id'],)).fetchone()['status'] == 'superseded'


@pytest.mark.parametrize('group_mode',[False,True])
def test_inactive_entry_contains_only_message_and_button(client,group_mode):
    item = create_group_link(client) if group_mode else create_main_assignment(client)
    param = 'group' if group_mode else 'invite'
    token = parse_qs(urlparse(item['invitation_url']).query)[param][0]
    assert 'participant-first-name' in client.get('/?'+param+'='+token).text
    with main.conn() as connection:
        connection.execute('UPDATE '+('experiment_group_links' if group_mode else 'experiment_assignments')+' SET active=0')
    result = client.get('/?'+param+'='+token)
    from html.parser import HTMLParser
    class Body(HTMLParser):
        inside=False
        tags=[]
        words=[]
        def handle_starttag(self,tag,attrs):
            if self.inside:self.tags.append(tag)
            if tag=='body':self.inside=True
        def handle_endtag(self,tag):
            if tag=='body':self.inside=False
        def handle_data(self,value):
            if self.inside and value.strip():self.words.append(value.strip())
    parsed=Body();parsed.feed(result.text)
    assert parsed.tags == ['p','button']
    assert parsed.words == ['این لینک غیر فعال است.','بستن صفحه']
    assert result.headers['cache-control'] == 'no-store'
    if group_mode:
        blocked=client.post('/api/experiment/group/claim',json={'group_token':token,'participant_code':'BLOCKED',**PROFILE})
    else:
        blocked=client.post('/api/experiment/consent',json={'invitation_token':token,'participant_code':item['participant_code'],'accepted':True,**PROFILE})
    assert blocked.status_code in (403,404) and blocked.headers['X-Barsan-Link-Inactive']=='1'


def test_browser_form_and_opponent_selection(browser_env):
    client,page,context,base=browser_env
    page.set_viewport_size({'width':390,'height':844})
    page.locator('#welcome-continue').click()
    for selector in ('#participant-first-name','#participant-last-name','#participant-code-input'):
        assert page.locator(selector).is_visible() and page.locator(selector).get_attribute('required') is not None
    assert page.locator('#group-code-choice').count()==0
    page.evaluate('ultimatumState={};utRenderWelcome()')
    page.get_by_role('button',name='شروع کنیم',exact=True).click()
    page.get_by_text('در حال انتخاب بازیکن مقابل شما از بین همکاران در سالن...',exact=True).wait_for()
    started=time.monotonic()
    assert page.get_by_role('button',name='شروع کنیم',exact=True).count()==0
    page.get_by_text('بازیکن مقابل شما انتخاب شد',exact=True).wait_for(timeout=7000)
    elapsed=time.monotonic()-started
    assert 2.8<=elapsed<=6.6
    page.get_by_role('button',name='شروع کنیم',exact=True).click()
    assert page.get_by_text('قبل از شروع بازی اصلی دو دور تمرینی انجام می‌شود که در نتیجه‌ی نهایی تاثیر ندارد.',exact=True).is_visible()
    assert page.evaluate("utWaitingMessage('agent')")=='منتظر دریافت پاسخ هوش مصنوعی بارسان باشید'
    assert page.evaluate("utWaitingMessage('coworker')")=='منتظر دریافت پاسخ همکارتان باشید'


def test_exact_requested_copy_and_no_unique_code_phrase():
    html = Path(main.ROOT,'app/index.html').read_text()
    for text in [
        'در هر دور، باید بین دریافت مبلغ کمتر همین الان و یا مبلغ بیشتر پس از مدت مشخص، یکی را انتخاب کنید.',
        'در این بازی مبالغ فرضی‌اند و پرداختی به شما صورت نمی‌گیرد',
        'هر وقت آماده بودید، بازی دوم را شروع کنید.',
        'منتظر دریافت پاسخ هوش مصنوعی بارسان باشید',
        'منتظر دریافت پاسخ همکارتان باشید',
        'در حال انتخاب بازیکن مقابل شما از بین همکاران در سالن...',
        'بازیکن مقابل شما انتخاب شد',
    ]:
        assert text in html
    assert 'کد یکتا' not in html
    assert 'منتظر پاسخ بازیکن مقابل' not in html


@pytest.mark.parametrize('group_mode',[False,True])
def test_busy_entry_never_mislabels_active_invitation(client,monkeypatch,group_mode):
    item=create_group_link(client) if group_mode else create_main_assignment(client)
    query=urlparse(item['invitation_url']).query
    def unavailable(*args,**kwargs):raise main.HTTPException(503,'Temporary database failure')
    monkeypatch.setattr(main,'_require_active_entry_token',unavailable)
    response=client.get('/?'+query)
    assert response.status_code==503
    assert 'این لینک غیر فعال است.' not in response.text
    assert 'participant-first-name' not in response.text
    assert 'در حال برقراری ارتباط' in response.text
    assert 2<=int(response.headers['refresh'])<=5
    assert response.headers['cache-control']=='no-store'


def test_browser_entry_retries_same_active_link_without_manual_refresh(browser_env,monkeypatch):
    client,page,_,base=browser_env
    group=client.post('/api/admin/group-links',headers={'X-Admin-Token':main.ADMIN_TOKEN},json={'scope':'test','label':'Synthetic automatic entry retry'}).json();query=urlparse(group['invitation_url']).query
    original=main._require_active_entry_token;attempts=[]
    def briefly_unavailable(*args,**kwargs):
        attempts.append(True)
        if len(attempts)==1:raise main.HTTPException(503,'Synthetic stale connection')
        return original(*args,**kwargs)
    monkeypatch.setattr(main,'_require_active_entry_token',briefly_unavailable)
    response=page.goto(base+'/?'+query)
    assert response.status==503
    page.wait_for_function("Boolean(document.querySelector('#participant-first-name'))",timeout=15000)
    assert page.url==base+'/?'+query and len(attempts)>=2
