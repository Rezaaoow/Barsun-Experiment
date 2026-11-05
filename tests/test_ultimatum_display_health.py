"""Small renderer-only health check. No participant requests or experiment writes."""
from test_browser_resilience import browser_env


def test_opponent_copy_and_frame_only_loss(browser_env):
    _, page, _, _ = browser_env
    result = page.evaluate('''()=>{
      const summary=[];
      for(const [module,condition,expected] of [
        ['ultimatum_frame','B',2],['ultimatum_control','A',1],['ultimatum','A',2],['ultimatum','B',1]]){
        Object.assign(workflow,{currentModuleCode:module,conditionCode:condition});
        ultimatumState={blockOrder:['proposer_agent'],blockIndex:0,lastResult:{block_score:8,block_potential_lost:12},blockSummaries:{},score:8,currentPhase:'main'};
        utRenderBlockSummary();
        summary.push({metrics:document.querySelectorAll('.ut-summary-metric').length,expected,
          lost:document.querySelectorAll('.ut-summary-metric.lost').length,
          recordedLoss:ultimatumState.blockSummaries.proposer_agent.potential_points_lost});
      }
      const copy=[];
      for(const condition of ['agent','coworker']){
        const name=condition==='agent'?'هوش‌مصنوعی بارسان':'همکارتان';
        const block='proposer_'+condition;
        ultimatumState={currentEventTimestamps:{},score:0,practiceScore:0,currentPhase:'main',blockOrder:[block],blockIndex:0};
        utRenderProposerOffer(false,block,1,1,{});
        const proposer=document.querySelector('#stimulus').textContent;
        const aria=document.querySelector('.ut-slider').getAttribute('aria-label');
        utRenderResponderDecision(false,'responder_'+condition,1,1,{participant_share:5,opponent_share:5});
        const responder=document.querySelector('#stimulus').textContent;
        utRenderBlockIntro();const intro=document.querySelector('#stimulus').textContent;
        copy.push({correct:proposer.includes('حاضرید چند امتیاز به '+name+' بدهید؟')&&proposer.includes('سهم '+name)&&aria.includes(name)&&responder.includes('پیشنهاد '+name)&&responder.includes('سهم '+name),
          noGeneric:![proposer,responder,intro].some(text=>text.includes('بازیکن مقابل'))});
      }
      return {summary,copy};
    }''')
    assert all(row['metrics']==row['expected'] and row['lost']==row['expected']-1 and row['recordedLoss']==12 for row in result['summary'])
    assert all(row['correct'] and row['noGeneric'] for row in result['copy'])
