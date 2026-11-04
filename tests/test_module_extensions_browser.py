import csv
import io
import json
from test_browser_resilience import browser_env
from module_extension_checks import create_assignment
import barsan_experiment as main


def test_standalone_dd_and_rest_use_real_durable_pipeline(browser_env):
    client, page, context, base = browser_env
    steps = [dict(step_id='dd-one', module_code='dd_standalone'),
             dict(step_id='rest', module_code='intermission'),
             dict(step_id='dd-two', module_code='dd_standalone')]
    identity = create_assignment(client, steps)
    info = client.post('/api/experiment/identify', json=identity).json()
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    # A committed DD completion whose HTTP acknowledgement is lost must replay safely.
    lost = []
    def lose_ack(route):
        if lost:
            route.continue_()
        else:
            response = route.fetch()
            assert response.status == 200
            lost.append(True)
            route.abort('failed')
    page.route('**/api/experiment/dd/complete', lose_ack)
    page.evaluate('''data=>{
      Object.assign(workflow,{participantCode:data.identity.participant_code,invitationToken:data.identity.invitation_token,
        assignmentId:data.info.assignment_id,workflowMode:true,timeTrackingEnabled:true,workflowSteps:data.info.workflow_steps,
        workflowStepIndex:0});
      window.__flowError=null;enterWorkflowStep().catch(error=>window.__flowError=error.message);
    }''', dict(identity=identity, info=info))
    for game in range(2):
        page.locator('.dd-guide .start-task-button').wait_for(state='visible')
        assert page.locator('.dd-guide .instruction-heading').inner_text() == ('بازی اول' if game == 0 else 'بازی دوم')
        page.locator('.dd-guide .start-task-button').click()
        for trial in range(5):
            choice = page.locator('.choice-card').first
            choice.wait_for(state='visible')
            text = page.locator('#stimulus').inner_text()
            assert '۵۰ میلیون تومان' in text and '۱۰۰ میلیون تومان' in text
            assert 'از عمرتان' not in text
            if game == 0 and trial == 1:
                context.set_offline(True)
            if game == 0 and trial == 4:
                assert page.evaluate('async()=> (await readPending()).length') >= 3
                context.set_offline(False)
            choice.click()
            page.wait_for_function('n=>trialCount===n', arg=trial+1)
            # Advance only when a new choice screen is constructed.
            if trial < 4:
                page.wait_for_function('()=>!document.querySelector(".choice-card")', timeout=5000)
        if game == 0:
            page.get_by_role('button', name='بازی دوم', exact=True).wait_for(timeout=20000)
            assert 'بازی اول تمام شد.' in page.locator('#stimulus').inner_text()
            context.set_offline(True)
            page.get_by_role('button', name='بازی دوم', exact=True).click()
            page.wait_for_function('''async()=> (await BarsanStore.list()).some(entry=>{
              const parts=JSON.parse(entry.scope);return parts[0]==='completion'&&entry.value.payload.stage_code==='intermission';
            })''')
            context.set_offline(False)
    page.wait_for_function('()=>document.querySelector("#finish:not(.hidden)")?.textContent.includes("برنامه پژوهشی تکمیل شد")', timeout=20000)
    assert not errors and page.evaluate('()=>window.__flowError') is None
    assert lost == [True]
    final = client.post('/api/experiment/identify', json=identity).json()
    assert all(step['status'] == 'completed' for step in final['workflow_steps'])
    with main.conn() as connection:
        rows = connection.execute('SELECT task_code,magnitude FROM lifetrade_trials').fetchall()
        assert len(rows) == 10 and all(row['task_code'] == 'KT' and row['magnitude'] == '100M' for row in rows)
        clocks = connection.execute('SELECT stage_code,closed FROM experiment_client_clocks').fetchall()
        assert len(clocks) == 3 and all(row['closed'] == 1 for row in clocks)
    assert page.evaluate('async()=> (await BarsanStore.list()).filter(e=>["dd","completion"].includes(JSON.parse(e.scope)[0])).length') == 0


def test_rest_numbering_excludes_questionnaires_and_repeated_rests(browser_env):
    _, page, _, _ = browser_env
    modules=['questionnaire','intermission','dd_standalone','intermission','questionnaire','intermission','ultimatum_control','intermission','dd_lifetrade','intermission']
    copies=page.evaluate('modules=>modules.map((m,i)=>intermissionCopy(modules.map(module_code=>({module_code})),i))', modules)
    assert 'پیش از ادامه' in copies[1]['paragraphs'][0]
    assert copies[1]['button'] == 'بازی اول'
    assert copies[3]['paragraphs'][0] == 'بازی اول تمام شد.'
    assert copies[5]['button'] == 'بازی دوم'
    assert copies[7]['button'] == 'بازی سوم'
    assert copies[9]['paragraphs'][0] == 'بازی سوم تمام شد.' and copies[9]['button'] == 'ادامه'
