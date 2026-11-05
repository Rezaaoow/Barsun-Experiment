"""Real Chrome/WebKit NEW Production journeys, synthetic group identities only."""
import asyncio,json,os,time
from pathlib import Path
from playwright.async_api import async_playwright
BASE='https://experiment.barsun4818.ir/dce'
ROOT=Path('/Users/rezaaoow/barsan-loadtest/final-acceptance-20261105/browser')
FIXTURE=json.loads((ROOT/'fixture.json').read_text());assert FIXTURE['synthetic']
results=[];progress={}
def report():
    (ROOT/'progress.json').write_text(json.dumps(progress));(ROOT/'results.json').write_text(json.dumps(results,indent=2))
async def step(page):
    choices=page.locator('.choice-card:visible:enabled')
    if await choices.count():await choices.first.click();return
    buttons=page.locator('#stimulus .ut-btn.accept:visible:enabled')
    if not await buttons.count():buttons=page.locator('#stimulus .next-page:visible:enabled, #stimulus .ut-btn:visible:enabled')
    if await buttons.count():
        label=await buttons.first.inner_text()
        if 'بستن' not in label and label!='تلاش دوباره':await buttons.first.click()
async def state(page):
    return await page.evaluate('''()=>({stage:workflow.currentStage,dd:trialCount,ut:ultimatumState?.results.length||0,run:ultimatumState?.runId||null,completed:Boolean(ultimatumState?.completed)})''')
async def advance(page,predicate,seconds=360):
    start=time.monotonic()
    while time.monotonic()-start<seconds:
        value=await state(page)
        if predicate(value):return value
        await step(page);await asyncio.sleep(.2)
    raise RuntimeError('Synthetic UI deadline')
async def one(pw,engine,index):
    start=time.monotonic();phase='launch';r={'engine':engine};browser=await getattr(pw,engine).launch(headless=True,**({'executable_path':'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'} if engine=='chromium' else {}))
    context=await browser.new_context(**(pw.devices['iPhone 13'] if engine=='webkit' else {}));page=await context.new_page();errors=[];page.on('pageerror',lambda e:errors.append(type(e).__name__))
    dropped=False;faults=0
    async def lose_ack(route):
        nonlocal dropped,faults
        if faults<(4 if engine=='chromium' else 1):
            if not dropped:
                response=await route.fetch();assert response.status==200;dropped=True
            faults+=1;await route.abort('failed')
        else:await route.continue_()
    await page.route(BASE+'/api/experiment/ultimatum/resolve',lose_ack)
    try:
        phase='entry';progress[engine]=phase;report();g=FIXTURE['groups'][index]
        await page.goto(BASE+'/?group='+g['group_token'],wait_until='networkidle')
        if await page.locator('#welcome-continue').is_visible():await page.locator('#welcome-continue').click()
        await page.locator('#participant-first-name').fill('Synthetic')
        await page.locator('#participant-last-name').fill('Browser')
        await page.locator('#participant-code-input').fill('SYN-FINAL-BROWSER-'+engine.upper())
        await page.locator('#consent-checkbox').check();await page.locator('#start').click()
        await page.wait_for_function("workflow.assignmentId&&document.querySelector('#ethics').classList.contains('hidden')",timeout=45000)
        phase='dd';progress[engine]=phase;report()
        await advance(page,lambda s:s['dd']==4)
        choice=page.locator('.choice-card:visible:enabled').first;await choice.wait_for(timeout=10000)
        await context.set_offline(True);await choice.click();await asyncio.sleep(5)
        r['dd_saved_offline']=await page.evaluate("async()=> (await BarsanStore.list()).some(e=>JSON.parse(e.scope)[0]==='dd')")
        assert r['dd_saved_offline'];await context.set_offline(False)
        phase='ut';progress[engine]=phase;report()
        await advance(page,lambda s:s['stage']=='ultimatum' and s['run'])
        await advance(page,lambda s:s['ut']==2)
        phase='idle';progress[engine]=phase;report()
        # Leave live origin and persistent DB handles idle through the load runs.
        idle=int(os.environ.get('IDLE_SECONDS','600'));await asyncio.sleep(idle);r['idle_seconds']=idle
        phase='finish-ut';progress[engine]=phase;report();await advance(page,lambda s:s['completed'],600)
        s=await state(page);assert s['ut']==22
        await page.evaluate('async()=>{await flushUtEvents();await BarsanClock.flush();}')
        pending=await page.evaluate("async()=> (await BarsanStore.list()).filter(e=>['ut','dd','completion','clock'].includes(JSON.parse(e.scope)[0])).length")
        assert pending==0 and not errors
        r.update(passed=True,ultimatum_rounds=22,pending_entries=pending,lost_ack_recovered=dropped,failed_resolve_attempts=faults,manual_retry_clicked=False,page_errors=len(errors),no_horizontal_overflow=await page.evaluate('document.documentElement.scrollWidth<=innerWidth+2'))
        progress[engine]='passed'
    except Exception as e:
        r.update(passed=False,phase=phase,error_type=type(e).__name__);progress[engine]='failed:'+phase
        # Keep diagnostics private; screenshots may contain synthetic credentials.
        await page.screenshot(path=str(ROOT/(engine+'-failure.png')))
    finally:
        r['duration_s']=round(time.monotonic()-start,1);results.append(r);report();await context.close();await browser.close()
async def main():
    async with async_playwright() as pw:await asyncio.gather(one(pw,'chromium',0),one(pw,'webkit',1))
    print(json.dumps(results));assert all(r['passed'] for r in results)
if __name__=='__main__':asyncio.run(main())
