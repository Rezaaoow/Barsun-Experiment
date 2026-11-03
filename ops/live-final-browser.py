"""Two explicitly authorized synthetic accounts; Staging only, real task delays."""
from pathlib import Path
import asyncio,json,time,subprocess
from playwright.async_api import async_playwright
BASE='https://staging.my-experiment.ir/lifetrade'
FIXTURE=Path('/Users/rezaaoow/barsan-loadtest/final-release-20261103/browser-two-accounts.json')
OUT=Path(__file__).resolve().parents[1]/'artifacts/final-release-20261103'
OUT.mkdir(parents=True,exist_ok=True)
SSH=['ssh','-o','BatchMode=yes','-o','IdentitiesOnly=yes','-o','StrictHostKeyChecking=yes','-o','UserKnownHostsFile=/Users/rezaaoow/.ssh/known_hosts_barsan_cpanel','-o','ConnectTimeout=10','-i','/Users/rezaaoow/.ssh/barsan_cpanel_ed25519','myexperi@my-experiment.ir']
progress={};results=[]
def report():
    (OUT/'browser-progress.json').write_text(json.dumps(progress,indent=2))
async def login(page,person):
    if await page.locator('#welcome-continue').is_visible():await page.locator('#welcome-continue').click()
    await page.locator('#participant-code-input').fill(person['participant_code'])
    await page.locator('#consent-checkbox').check();await page.locator('#start').click()
    await page.wait_for_function("workflow.participantCode&&workflow.assignmentId&&document.querySelector('#ethics').classList.contains('hidden')",timeout=60000)
async def step(page):
    choice=page.locator('.choice-card:enabled').first
    if await choice.count() and await choice.is_visible():await choice.click();return
    button=page.locator('#stimulus .ut-btn.accept:visible:enabled').first
    if not await button.count():button=page.locator('#stimulus .next-page:visible:enabled, #stimulus .ut-btn:visible:enabled').first
    if await button.count():
        if 'پایان و بستن' not in await button.inner_text():await button.click()
async def state(page):
    return await page.evaluate("""()=>({dd:trialCount,stage:workflow.currentStage,run:workflow.timerRunId,
      ddRun:typeof sessionId==='string'?sessionId:null,utRun:ultimatumState?.runId||null,
      ut:ultimatumState?.results.length||0,completed:Boolean(ultimatumState?.completed)})""")
async def wait_advance(page,predicate,limit=300):
    start=time.monotonic()
    while time.monotonic()-start<limit:
        s=await state(page)
        if predicate(s):return s
        await step(page);await asyncio.sleep(.18)
    raise RuntimeError('Synthetic UI phase deadline')
async def one(pw,person,engine):
    phase='launch';start=time.monotonic();r={'engine':engine,'mobile_profile':engine=='webkit','prescribed_delays_unchanged':True}
    browser=await getattr(pw,engine).launch(headless=True,**({'executable_path':'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'} if engine=='chromium' else {}))
    context=await browser.new_context(**(pw.devices['iPhone 13'] if engine=='webkit' else {}))
    page=await context.new_page();page.set_default_timeout(10000)
    page_errors=[];page.on('pageerror',lambda e:page_errors.append(type(e).__name__))
    dropped=False;slowed=False
    async def fault_route(route):
        nonlocal dropped,slowed
        path=route.request.url.split('?')[0]
        if engine=='webkit' and path.endswith('/ultimatum/resolve') and not dropped:
            dropped=True
            await route.fetch(timeout=20000);await route.abort('failed');return
        if engine=='webkit' and path.endswith('/ultimatum/events/batch') and not slowed:
            slowed=True;await asyncio.sleep(8)
        await route.continue_()
    await page.route(BASE+'/api/**',fault_route)
    try:
        phase='entry';progress[engine]=phase;report()
        await page.goto(person['invitation_url'],wait_until='networkidle',timeout=30000);await login(page,person)
        if engine=='chromium':
            phase='dd-offline-reload';progress[engine]=phase;report()
            old=await wait_advance(page,lambda s:s['stage']=='dd_lifetrade' and s['dd']==4)
            await page.locator('.choice-card:enabled').first.wait_for(timeout=15000)
            await context.set_offline(True);await page.locator('.choice-card:enabled').first.click()
            for _ in range(19):await asyncio.sleep(5)
            pending=await page.evaluate("async()=> (await BarsanStore.list()).filter(e=>JSON.parse(e.scope)[0]==='dd').length")
            assert pending>0
            await context.set_offline(False);await page.reload(wait_until='networkidle');await login(page,person)
            fresh=await wait_advance(page,lambda s:s['stage']=='dd_lifetrade' and s['run'] and s['run']!=old['run'],60)
            assert fresh['dd']==0
            r.update(dd_offline_seconds=95,old_dd_answer_retained=True,fresh_attempt_after_reload=True)
        else:
            phase='dd-brief-outage';progress[engine]=phase;report()
            await wait_advance(page,lambda s:s['stage']=='dd_lifetrade' and s['dd']==4)
            await context.set_offline(True)
            for _ in range(50):await step(page);await asyncio.sleep(.2)
            await context.set_offline(False);r['dd_offline_seconds']=10
        phase='full-dd';progress[engine]=phase;report()
        await wait_advance(page,lambda s:s['stage']=='ultimatum' and s['utRun'],300)
        r['dd_completed_via_ui']=True
        if engine=='chromium':
            phase='ut-pending-host-restart';progress[engine]=phase;report()
            button=page.get_by_role('button',name='ارسال پیشنهاد',exact=True)
            for _ in range(100):
                if await button.count() and await button.is_visible():break
                await step(page);await asyncio.sleep(.2)
            assert await button.is_visible()
            old=(await state(page))['utRun']
            await context.set_offline(True);await button.click();await asyncio.sleep(3)
            pending=await page.evaluate("async()=> (await BarsanStore.list()).filter(e=>JSON.parse(e.scope)[0]==='ut'&&e.value.kind==='resolve').length")
            assert pending>0
            command='/home/myexperi/virtualenv/lifetrade-staging/3.12/bin/supervisorctl -c /home/myexperi/lifetrade-staging/native-asgi-trial-20261102/supervisor.ini restart barsan-native'
            child=await asyncio.create_subprocess_exec(*SSH,command,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
            await asyncio.wait_for(child.communicate(),timeout=50);assert child.returncode==0
            r['actual_staging_restart_with_pending_decision']=True
            progress[engine]='staging-restart-completed';report()
            for _ in range(13):await asyncio.sleep(5)
            await context.set_offline(False);await page.reload(wait_until='networkidle');await login(page,person)
            await wait_advance(page,lambda s:s['utRun'] and s['utRun']!=old,90)
            r['old_ut_decision_recovered_before_new_attempt']=True
        phase='full-ut';progress[engine]=phase;report()
        screenshot=False
        for _ in range(1800):
            s=await state(page)
            if s['completed']:break
            if engine=='webkit' and not screenshot and await page.locator('.ut-slider').count():
                await page.screenshot(path=str(OUT/'staging-webkit-mobile.png'))
                r['no_horizontal_overflow']=await page.evaluate('document.documentElement.scrollWidth<=innerWidth+2')
                screenshot=True
            await step(page);await asyncio.sleep(.2)
        else:raise RuntimeError('Full UT UI deadline')
        assert s['ut']==22
        await page.evaluate('async()=>{await flushUtEvents();await BarsanClock.flush();}')
        verified=await context.request.post(BASE+'/api/experiment/identify',data={'participant_code':person['participant_code'],'invitation_token':person['invitation_token']})
        assert verified.status==200
        identity=await verified.json();assert all(step['status']=='completed' for step in identity['workflow_steps'])
        pending=await page.evaluate("async()=> (await BarsanStore.list()).filter(e=>['ut','dd','completion','clock'].includes(JSON.parse(e.scope)[0])).length")
        r.update(passed=True,ultimatum_ui_rounds=22,workflow_steps_completed=2,pending_entries=pending,
                 lost_ack_retried=dropped,slow_auxiliary_injected=slowed,page_error_count=len(page_errors))
        assert pending==0 and not page_errors
        progress[engine]='passed';report()
    except Exception as exc:
        r.update(passed=False,failed_phase=phase,error_type=type(exc).__name__);progress[engine]='failed:'+phase;report()
    finally:
        r['duration_seconds']=round(time.monotonic()-start,2);results.append(r)
        (OUT/'live-browser-results.json').write_text(json.dumps(results,indent=2))
        await context.close();await browser.close()
async def main():
    fixture=json.loads(FIXTURE.read_text());assert fixture['base_url']==BASE and len(fixture['participants'])==2
    async with async_playwright() as pw:
        await asyncio.gather(one(pw,fixture['participants'][0],'chromium'),one(pw,fixture['participants'][1],'webkit'))
    print(json.dumps(results));assert len(results)==2 and all(r['passed'] for r in results)
if __name__=='__main__':asyncio.run(main())
