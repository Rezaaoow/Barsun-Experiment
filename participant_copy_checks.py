"""Public UI verification: no identity submission, consent acceptance or trial writes."""
from pathlib import Path

SUMMARY='بارسانی عزیز، شما در این برنامه دو بازی انجام می‌دهید و امتیاز به دست آمده شما در پایان بازی دوم به حساب شما در باشگاه رانندگان واریز می‌شود.'
EXPLANATION='این بازی‌ها یرای یک فعالیت پژوهشی دربارهٔ تجربهٔ همکاری و شرایط کاری شما در بارسان انجام می‌شود. داده‌های گردآوری‌شده در راستای اهداف پژوهشی استفاده می‌شوند و نتایج در سطح گروهی گزارش خواهند شد و در اختیار تیم پژوهشی می‌ماند.'


def check_copy(page,url,*,artifacts=None,title_preview=False):
    errors=[];outline=[]
    page.on('pageerror',lambda error:errors.append(type(error).__name__))
    page.on('request',lambda request:outline.append(1) if '/api/experiment/workflow' in request.url else None)
    for label,width,height in [('desktop',1096,807),('mobile',390,844)]:
        page.set_viewport_size({'width':width,'height':height})
        page.goto(url,wait_until='networkidle')
        assert page.locator('#welcome .brand-mark, #welcome .eyebrow, #welcome ol').count()==0
        assert page.locator('#welcome .welcome-summary').inner_text()==SUMMARY
        assert page.evaluate('document.documentElement.scrollWidth<=window.innerWidth+1')
        if artifacts:
            root=Path(artifacts);root.mkdir(parents=True,exist_ok=True)
            page.screenshot(path=str(root/(label+'-welcome.png')),full_page=True)
        page.locator('#welcome-continue').click()
        assert page.locator('#ethics').is_visible()
        assert page.locator('#ethics .eyebrow').count()==0
        assert page.locator('#ethics > p:not([id])').all_text_contents()==[EXPLANATION]
        assert page.locator('#participant-code-instruction').is_visible()
        assert page.locator('#group-code-choice').count()==0
        for selector in ('#participant-first-name','#participant-last-name','#participant-code-input'):
            assert page.locator(selector).is_visible()
            assert page.locator(selector).is_enabled()
            assert page.locator(selector).get_attribute('required') is not None
        assert page.evaluate('document.documentElement.scrollWidth<=window.innerWidth+1')
        assert not page.locator('#consent-checkbox').is_checked()
        if artifacts:page.screenshot(path=str(root/(label+'-explanation.png')),full_page=True)
        page.locator('#ethics-back').click()
        assert page.locator('#welcome').is_visible()
    assert not outline and not errors
    if title_preview:
        # Local renderer preview only; this never starts a server attempt.
        page.evaluate("""()=>{
          Object.assign(workflow,{workflowMode:true,workflowStepIndex:0,currentModuleCode:'dd_standalone',workflowSteps:[
           {module_code:'dd_standalone'},{module_code:'intermission'},{module_code:'questionnaire'},
           {module_code:'ultimatum_control'},{module_code:'dd_standalone'}]});
          show();showInstructionPage('KT',0);
        }""")
        assert page.locator('.instruction-heading').inner_text()=='بازی اول'
        page.evaluate("()=>{workflow.workflowStepIndex=4;showInstructionPage('KT',0);}")
        assert page.locator('.instruction-heading').inner_text()=='بازی سوم'
    return dict(passed=True,viewports=2,group_controls_work=True,automatic_consent=False,
                workflow_outline_requests=len(outline),page_errors=len(errors))
