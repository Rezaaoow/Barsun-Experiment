"""Focused renderer/transition check; no live participants or task submissions."""
import os
from pathlib import Path
from test_browser_resilience import browser_env


def test_one_dd_guide_and_direct_practice_responder(browser_env):
    _,page,_,_=browser_env
    page.evaluate("""()=>{
      Object.assign(workflow,{workflowMode:true,currentModuleCode:'dd_standalone',workflowStepIndex:0,
        workflowSteps:[{module_code:'dd_standalone'}]});
      ultimatumState={score:0,practiceScore:0,currentPhase:'practice',currentEventTimestamps:{}};
      show();showInstructionPage('KT',0);
    }""")
    assert page.evaluate('INSTRUCTION_PAGE_COUNT')==1
    assert page.locator('.instruction-heading').inner_text()=='بازی اول'
    assert page.locator('.instruction-progress').count()==0
    assert page.locator('.start-task-button').inner_text()=='شروع بازی'
    captures=Path(os.environ['BARSAN_UI_ARTIFACTS']) if os.environ.get('BARSAN_UI_ARTIFACTS') else None
    if captures:captures.mkdir(parents=True,exist_ok=True)
    for name,width,height in [('desktop',1096,807),('mobile',390,844)]:
        page.set_viewport_size({'width':width,'height':height})
        page.evaluate("()=>{show();showInstructionPage('KT',0);}")
        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
        assert page.locator('.instruction-heading').evaluate('(el)=>parseFloat(getComputedStyle(el).fontSize)')>=40
        if name=='desktop':
            assert page.locator('.start-task-button').evaluate('(el)=>{const r=el.getBoundingClientRect();return r.bottom<=innerHeight && r.top>=0;}')
        if captures:page.screenshot(path=str(captures/(name+'-dd-guide.png')))
        page.evaluate("()=>utRenderProposerOffer(true,'practice_proposer_agent',0,1,{})")
        assert page.locator('.ut-opponent-statement').evaluate('(el)=>parseFloat(getComputedStyle(el).fontSize)')>=23
        assert page.locator('.ut-question').inner_text()=='حاضرید چند امتیاز به هوش‌مصنوعی بارسان بدهید؟'
        page.locator('.ut-btn').scroll_into_view_if_needed()
        assert page.locator('.ut-btn').is_visible()
        if captures:page.screenshot(path=str(captures/(name+'-proposer.png')))
    result=page.evaluate("""async()=>{
      const prepare=utPrepare,wait=utWaitThenResponder;
      const calls=[];
      try{
        utPrepare=async(...args)=>{calls.push(['prepare',...args]);return {round_id:'local-preview',participant_share:8,opponent_share:2};};
        utWaitThenResponder=async(...args)=>{calls.push(['wait',...args.slice(0,4)]);utRenderResponderDecision(...args);};
        await utStartPracticeResponder();
        return calls;
      }finally{utPrepare=prepare;utWaitThenResponder=wait;}
    }""")
    assert result==[['prepare','practice','practice_responder_coworker',0,1,0],['wait',True,'practice_responder_coworker',0,1]]
    assert page.get_by_role('button',name='آماده‌ام').count()==0
    assert page.locator('.ut-shares .ut-metric-label').all_text_contents()==['سهم شما','سهم همکارتان']
    page.get_by_role('button',name='رد',exact=True).scroll_into_view_if_needed()
    assert page.get_by_role('button',name='قبول',exact=True).is_visible()
    assert page.get_by_role('button',name='رد',exact=True).is_visible()
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
    if captures:page.screenshot(path=str(captures/'mobile-responder.png'))
