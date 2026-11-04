"""Local visual preview only. Serves existing renderers, blocks all POST requests."""
import argparse,json
from http.server import ThreadingHTTPServer,BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse,parse_qs

parser=argparse.ArgumentParser();parser.add_argument('--port',type=int,default=8892);args=parser.parse_args()
root=Path(__file__).resolve().parents[1]
fixture=r'''
<script>
// Disposable local visual fixtures. No invitation, participant or server run exists.
window.addEventListener('DOMContentLoaded',()=>{
 document.addEventListener('click',event=>{if(event.target.closest('button')){event.preventDefault();event.stopImmediatePropagation();}},true);
 Object.assign(workflow,{workflowMode:true,currentModuleCode:'dd_standalone',workflowStepIndex:0,
  workflowSteps:[{module_code:'dd_standalone'},{module_code:'intermission'},{module_code:'ultimatum_control'}],
  participantCode:'LOCAL-VISUAL',questionnaireAnswers:{},questionnaireSavedAnswers:{}});
 ultimatumState={score:14,practiceScore:5,currentPhase:'main',currentEventTimestamps:{},
  blockOrder:['proposer_agent','responder_coworker','proposer_coworker','responder_agent'],blockIndex:0,
  blockSummaries:{},results:[],lastResult:{block_score:14,block_potential_lost:9}};
 const screen=new URLSearchParams(location.search).get('screen')||'welcome';
 const round={participant_share:5,opponent_share:5,round_id:'local-visual'};
 if(screen==='ethics')showPanel('ethics');
 else if(screen==='dd-guide'){show();showInstructionPage('KT',0);}
 else if(screen==='lt-guide'){workflow.currentModuleCode='dd_lifetrade';show();showInstructionPage('LT',0);}
 else if(screen==='dd-choice'){show();makeTrialScreen('KT','100M',{delay:'1 year'},true);}
 else if(screen==='lt-choice'){show();makeTrialScreen('LT','1M',{delay:'1 year'},false);}
 else if(screen==='rest'){const copy=intermissionCopy(workflow.workflowSteps,1);show();showInfo('استراحت',copy.paragraphs);document.querySelector('.next-page').textContent=copy.button;}
 else if(screen==='ut-welcome')utRenderWelcome();
 else if(screen==='ut-practice')utRenderPracticeIntro();
 else if(screen==='ut-proposer')utRenderProposerOffer(false,'proposer_agent',1,1,round);
 else if(screen==='ut-responder')utRenderResponderDecision(false,'responder_coworker',2,1,round);
 else if(screen==='ut-block-agent')utRenderBlockIntro();
 else if(screen==='ut-block-coworker'){ultimatumState.blockIndex=1;utRenderBlockIntro();}
 else if(screen==='ut-wait')utRenderWaiting('coworker','لطفاً منتظر پیشنهاد بازیکن مقابل باشید…');
 else if(screen==='ut-result')utRenderResult(false,'responder_coworker',2,1,{accepted:true,points_earned:5});
 else if(screen==='ut-summary')utRenderBlockSummary();
 else if(screen==='questionnaire-intro')showQuestionnaireIntro(0);
 else if(screen==='questionnaire-question')showQuestionnaireQuestion(0);
 else if(screen==='finish')showPanel('finish');
 document.documentElement.dataset.localVisualReady=screen;
});
</script>
'''
class Handler(BaseHTTPRequestHandler):
 def log_message(self,*args):pass
 def do_POST(self):self.send_error(405)
 def do_GET(self):
  u=urlparse(self.path);query=parse_qs(u.query);source=root.parent/'admin-repair' if query.get('baseline')==['1'] else root
  if u.path=='/assets/vazirmatn-v33.003.woff2':
   data=(root/'app/fonts/vazirmatn-v33.003.woff2').read_bytes();kind='font/woff2'
  elif u.path=='/':
   text=(source/'app/index.html').read_text().replace('<!-- BARSAN_PERSISTENCE -->','<script>'+(source/'app/persistence.js').read_text()+'</script><script>'+(source/'app/task_timing.js').read_text()+'</script>',1).replace('__BARSAN_FONT_URL__','/assets/vazirmatn-v33.003.woff2')
   data=text.replace('</body>',fixture+'</body>').encode();kind='text/html; charset=utf-8'
  else:self.send_error(404);return
  self.send_response(200);self.send_header('Content-Type',kind);self.send_header('Cache-Control','no-store');self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
print(json.dumps({'local_visual_preview':True,'port':args.port,'all_posts_blocked':True}),flush=True)
ThreadingHTTPServer(('127.0.0.1',args.port),Handler).serve_forever()
