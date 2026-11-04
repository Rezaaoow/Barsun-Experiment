"""Disposable admin UI regression; synthetic local token and invitations only."""
import json
import uuid
from urllib.parse import parse_qs,urlparse
from test_browser_resilience import browser_env
import barsan_experiment as main


def test_admin_buttons_refresh_and_ignore_stale_scope(browser_env):
    client,page,_,base=browser_env
    headers={'X-Admin-Token':main.ADMIN_TOKEN}
    group=client.post('/api/admin/group-links',headers=headers,json={'scope':'main','label':'Synthetic UI admin group'}).json()
    token=parse_qs(urlparse(group['invitation_url']).query)['group'][0]
    person=client.post('/api/experiment/group/claim',json={'group_token':token,'assign_random':True,'claim_request_id':str(uuid.uuid4())}).json()
    stale=client.get('/api/admin/assignments?scope=main',headers=headers).json()
    errors=[];bad=[]
    page.on('pageerror',lambda e:errors.append(type(e).__name__))
    page.on('response',lambda r:bad.append(r.status) if '/api/admin/' in r.url and r.status>=400 else None)
    page.on('dialog',lambda d:d.accept('DELETE GROUP LINKS' if 'DELETE GROUP LINKS' in d.message else 'DELETE INACTIVE LINKS') if d.type=='prompt' else d.accept())
    page.goto(base+'/admin')
    page.locator('#adminToken').fill(main.ADMIN_TOKEN)
    page.locator('#connectButton').click()
    row=page.locator('#assignmentManagementTable tr').filter(has_text=person['participant_code'])
    row.wait_for()
    delayed=[]
    def hold_first_main(route):
        if 'scope=main' in route.request.url and not delayed:delayed.append(route)
        else:route.continue_()
    page.route('**/api/admin/assignments?**',hold_first_main)
    page.locator('#refreshAssignments').click()
    page.wait_for_function('document.querySelector("#refreshAssignments").disabled')
    page.locator('#monitorScope').select_option('test')
    page.locator('#assignmentManagementTable').get_by_text('در این محدوده هنوز لینکی ساخته نشده است.').wait_for()
    assert delayed
    delayed[0].fulfill(status=200,content_type='application/json',body=json.dumps(stale))
    page.evaluate('async()=>{await new Promise(requestAnimationFrame);await new Promise(requestAnimationFrame);}')
    assert page.locator('#assignmentManagementTable tr').count()==0
    page.locator('#monitorScope').select_option('main')
    row.wait_for()
    group_row=page.locator('#groupLinksTable tr').filter(has_text='Synthetic UI admin group')
    group_row.get_by_role('button',name='بستن ورود جدید',exact=True).click()
    group_row.get_by_role('button',name='بازکردن ورود جدید',exact=True).wait_for()
    group_row.get_by_role('button',name='حذف لینک گروهی',exact=True).click()
    page.locator('#groupLinksTable').get_by_text('هنوز لینک گروهی در این محدوده ساخته نشده است.').wait_for()
    row.get_by_role('button',name='غیرفعال‌کردن',exact=True).click()
    row.get_by_role('button',name='فعال‌سازی',exact=True).wait_for()
    row.get_by_role('button',name='حذف لینک',exact=True).click()
    page.locator('#assignmentManagementTable').get_by_text('در این محدوده هنوز لینکی ساخته نشده است.').wait_for()
    page.locator('#previewReset').click()
    page.locator('#maintenancePreview').wait_for(state='visible')
    page.locator('#confirmPhrase').fill('CLEAR MAIN DATA')
    page.locator('#resetData').click()
    page.locator('#maintenanceStatus').get_by_text('پاک‌سازی انجام شد.',exact=False).wait_for()
    assert not errors and not bad
