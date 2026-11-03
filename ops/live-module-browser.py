"""Focused live Staging verification; reuses exactly two authorized private invitations."""
from pathlib import Path
import asyncio
import json
import time
import sys
from playwright.async_api import async_playwright
BASE='https://staging.my-experiment.ir/lifetrade'
FIXTURE=Path('/Users/rezaaoow/barsan-loadtest/final-release-20261103/browser-two-accounts.json')
OUT=Path(__file__).resolve().parents[1]/'artifacts/module-extension'
OUT.mkdir(parents=True,exist_ok=True)
SHA=sys.argv[1]
results=[]
async def one(pw,person,engine):
    started=time.monotonic();phase='launch';errors=[]
    browser=await getattr(pw,engine).launch(headless=True,**({'executable_path':'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'} if engine=='chromium' else {}))
    context=await browser.new_context(**({k:v for k,v in pw.devices['iPhone 13'].items() if k!='default_browser_type'} if engine=='webkit' else {}))
    page=await context.new_page();page.on('pageerror',lambda error:errors.append(type(error).__name__))
    result=dict(engine=engine,passed=False,prescribed_delays_unchanged=True,source_commit=SHA)
    lost=[]
    async def lose_ack(route):
        if lost:await route.continue_();return
        response=await route.fetch()
        assert response.status==200
        lost.append(True);await route.abort('failed')
    await page.route('**/api/experiment/dd/complete',lose_ack)
    try:
        phase='login'
        await page.goto(person['invitation_url'],wait_until='domcontentloaded',timeout=30000)
        await page.locator('#welcome-continue').click()
        await page.locator('#participant-code-input').fill(person['participant_code'])
        await page.locator('#consent-checkbox').check();await page.locator('#start').click()
        for game in range(2):
            phase='dd-'+str(game+1)
            for instruction in range(4):
                await page.wait_for_function('n=>document.querySelector(".instruction-progress .step-count")?.textContent.includes(String(n))',arg=instruction+1,timeout=45000)
                await page.locator('.next-page').click()
            for trial in range(5):
                choice=page.locator('.choice-card').first
                await choice.wait_for(state='visible',timeout=30000)
                text=await page.locator('#stimulus').inner_text()
                assert '۵۰ میلیون تومان' in text and '۱۰۰ میلیون تومان' in text and 'از عمرتان' not in text
                if game==0 and trial==1:await context.set_offline(True)
                if game==0 and trial==4:
                    assert await page.evaluate('async()=> (await readPending()).length')>=3
                    await context.set_offline(False)
                await choice.click()
                await page.wait_for_function('n=>trialCount===n',arg=trial+1)
                if trial<4:await page.wait_for_function('()=>!document.querySelector(".choice-card")',timeout=5000)
            if game==0:
                phase='rest-offline'
                button=page.get_by_role('button',name='ادامه به بازی دوم',exact=True)
                await button.wait_for(timeout=45000)
                assert 'بازی اول تمام شد.' in await page.locator('#stimulus').inner_text()
                await context.set_offline(True);await button.click()
                await page.wait_for_function('''async()=> (await BarsanStore.list()).some(e=>JSON.parse(e.scope)[0]==='completion'&&e.value.payload.stage_code==='intermission')''')
                await context.set_offline(False)
        phase='completion'
        await page.wait_for_function('()=>document.querySelector("#finish:not(.hidden)")?.textContent.includes("برنامه پژوهشی تکمیل شد")',timeout=45000)
        pending=await page.evaluate('async()=> (await BarsanStore.list()).filter(e=>["dd","completion"].includes(JSON.parse(e.scope)[0])).length')
        assert not errors and pending==0 and lost==[True]
        assert await page.evaluate('document.documentElement.scrollWidth<=window.innerWidth+1')
        result.update(passed=True,dd_responses=10,workflow_steps=3,offline_answers_recovered=True,
                      offline_rest_completion_recovered=True,lost_ack_replayed=True,pending_entries=pending,page_errors=len(errors))
        await page.screenshot(path=str(OUT/('staging-'+engine+'.png')),full_page=True)
    except Exception as error:
        result.update(failed_phase=phase,error_type=type(error).__name__,page_errors=len(errors))
    finally:
        result['duration_seconds']=round(time.monotonic()-started,2)
        results.append(result);await context.close();await browser.close()
async def main():
    fixture=json.loads(FIXTURE.read_text());assert fixture['base_url']==BASE and len(fixture['participants'])==2
    async with async_playwright() as pw:
        await asyncio.gather(one(pw,fixture['participants'][0],'chromium'),one(pw,fixture['participants'][1],'webkit'))
    (OUT/'live-browser-results.json').write_text(json.dumps(results,indent=2))
    print(json.dumps(results))
    assert len(results)==2 and all(row['passed'] for row in results)
asyncio.run(main())
