// Execute the actual PR25 queue functions with a controlled network and storage.
// This is a JS fault-injection check, not a substitute for browser UI testing.
import fs from 'node:fs';
import vm from 'node:vm';
import assert from 'node:assert/strict';
import { randomUUID } from 'node:crypto';

const html = fs.readFileSync(new URL('../app/index.html', import.meta.url), 'utf8');
function section(first, last) {
  const start = html.indexOf(first);
  const end = html.indexOf(last, start + first.length);
  assert(start >= 0 && end > start);
  return html.slice(start, end);
}
const values = {};
const localStorage = new Proxy({}, {
  get(_, key) {
    if (key === 'getItem') return key => values[key] ?? null;
    if (key === 'setItem') return (key, value) => { values[key] = value; };
    if (key === 'removeItem') return key => { delete values[key]; };
  },
  ownKeys() { return Object.keys(values); },
  getOwnPropertyDescriptor() { return { enumerable: true, configurable: true }; }
});
const timers = new Map();
let timerId = 0;
let failing = true;
let requests = 0;
const sent = [];
const entries=new Map();
const BarsanStore={
 scope:(...parts)=>JSON.stringify(parts),
 async put(scope,key,value,mode='immutable'){
  const id=JSON.stringify([scope,key]),previous=entries.get(id);
  let merged=previous&&mode==='immutable'?previous.value:value;
  if(previous&&mode==='events')merged={...previous.value,...value,payload:{...previous.value.payload,...value.payload,event_timestamps:{...value.payload.event_timestamps,...previous.value.payload.event_timestamps}}};
  const entry={id,scope,key,revision:(previous?.revision||0)+1,value:merged};entries.set(id,entry);return entry;
 },
 async list(scope){return [...entries.values()].filter(entry=>scope===undefined||entry.scope===scope);},
 async ack(sent){for(const entry of sent)if(entries.get(entry.id)?.revision===entry.revision)entries.delete(entry.id);},
 async initialize(){},async acquire(){},async migrate(){},
};
const context = vm.createContext({
 BarsanClock:{currentRun:()=>null,pending:async()=>[],acknowledge:async()=>{},flush:async()=>{}},
 BarsanStore,BarsanTransport:{run:work=>work()},
  console: { warn() {}, error() {} },
  localStorage, crypto: { randomUUID }, Date, Math, Set, Map, Number, JSON,
  AbortController,
  workflow: { participantCode: 'SYNTHETIC', invitationToken: 'synthetic-test-token', stageRunId: 'questionnaire-1', currentStepId: 'step-q', currentStage: 'questionnaire', questionnaireAnswers: {}, questionnaireSavedAnswers: {}, questionIndex: 0 },
  sessionId: 'dd-1',
  APP_BASE_PATH: '',
  QUESTIONNAIRE_ITEMS: [{ code: 'B1' }, { code: 'B2' }],
  stimulus: { querySelector() { return null; } },
  window: { setTimeout(fn, delay) { const id = ++timerId; timers.set(id, { fn, delay }); return id; }, clearTimeout(id) { timers.delete(id); }, addEventListener() {} },
  document: { addEventListener() {}, hidden: false },
  setStatus() {}, nowISO() { return new Date().toISOString(); },
  async utDelay() {},
  async fetch(path, options) {
    requests++;
    sent.push({ path, payload: JSON.parse(options.body) });
    if (failing) throw new Error('synthetic network interruption');
    return { ok: true, status: 200, headers: { get() { return null; } }, async json() { return { saved: true }; } };
  },
});
const sources = [
  section('async function api(path, body, options={})', 'let workflowTimerHeartbeat='),
  section('function timePendingStorageKey()', 'async function postStepTime('),
  section('const TRIAL_BATCH_SIZE=', 'function makeTrialScreen('),
  section('function questionnairePendingStorageKey()', 'function updateQuestionnaireSelection('),
  section('function utOutboxPrefix()', 'async function startUltimatumInstructions('),
  `async function utApiWithRetry(path,payload,attempts=3){return apiWithRetry(path,payload,{attempts});}`,
];
vm.runInContext(sources.join('\n'), context);
async function run(code) { return vm.runInContext(`(async()=>{${code}})()`, context); }

await run(`await saveTrial({client_event_id:'dd-event-1',reaction_time_ms:500});`);
assert.equal(await run('return (await readPending()).length;'),1);
await run(`await saveTrial({client_event_id:'dd-event-1',reaction_time_ms:500});`);
assert.equal(await run('return (await readPending()).length;'),1);
let before = requests;
assert.equal(await run('return await flushPending();'),1);
assert.equal(requests-before,3);
assert.equal(await run('return (await readPending()).length;'),1);
failing=false;
assert.equal(await run('return await flushPending();'),0);
console.log('PASS DD: store-before-send, duplicate suppression, 3-attempt cap, recovery');

await run(`queueQuestionnaireAnswer({code:'B1'},0,'1','step-q'); queueQuestionnaireAnswer({code:'B1'},0,'2','step-q');`);
const qKey = 'barsan_questionnaire_pending_SYNTHETIC_questionnaire-1';
assert.equal(JSON.parse(values[qKey]).length, 1);
assert.equal(JSON.parse(values[qKey])[0].response_value, '2');
failing = true;
before = requests;
assert.equal(await run('return await flushQuestionnairePending();'), 1);
assert.equal(requests - before, 3);
assert.equal(JSON.parse(values[qKey]).length, 1);
failing = false;
await run(`workflow.stageRunId='questionnaire-2'; return await recoverOrphanedQuestionnaireQueues();`);
assert.equal(values[qKey], undefined);
assert.equal(sent.at(-1).payload.run_id, 'questionnaire-1');
console.log('PASS questionnaire: latest answer wins locally, failed sends persist, and old-run recovery keeps its run ID');

await run(`queueTimeEvent('start');queueTimeEvent('heartbeat');`);
const tKey = 'barsan_time_pending_SYNTHETIC';
const timeIds = JSON.parse(values[tKey]).map(row => row.event_id);
failing = true;
await assert.rejects(run('return await flushTimePending();'));
assert.equal(JSON.parse(values[tKey]).length, 2);
failing = false;
await run('return await flushTimePending();');
assert.equal(values[tKey], undefined);
assert.deepEqual(sent.slice(-2).map(row => row.payload.event_id), timeIds);
console.log('PASS time tracking: failed sends persist and recovery preserves event IDs');

await run(`await utQueueOutboxItem('ut-1',{kind:'resolve',item_id:'round-1',payload:{run_id:'ut-1',round_id:'round-1',invitation_token:'synthetic-test-token',participant_code:'SYNTHETIC',participant_decision:'accept'}});`);
assert.equal(await run(`return (await utReadOutboxKey(utOutboxKey('ut-1')))[0].payload.invitation_token;`),undefined);
failing=true;
await assert.rejects(run(`return await utSendOutboxItem('ut-1',(await utReadOutboxKey(utOutboxKey('ut-1')))[0]);`));
assert.equal(await run(`return (await utReadOutboxKey(utOutboxKey('ut-1'))).length;`),1);
failing=false;
await run(`return await utSendOutboxItem('ut-1',(await utReadOutboxKey(utOutboxKey('ut-1')))[0]);`);
assert.equal(await run(`return (await utReadOutboxKey(utOutboxKey('ut-1'))).length;`),0);
await run(`await utQueueOutboxItem('ut-1',{kind:'events',item_id:'round-1',payload:{event_timestamps:{advance_clicked_at:'2026-11-03T10:00:00Z'}}});await utQueueOutboxItem('ut-1',{kind:'events',item_id:'round-1',payload:{event_timestamps:{advance_clicked_at:'2026-11-03T10:00:05Z'}}});`);
assert.equal(await run(`return (await utReadOutboxKey(utOutboxKey('ut-1')))[0].payload.event_timestamps.advance_clicked_at;`),'2026-11-03T10:00:00Z');
console.log('PASS Ultimatum: credentials omitted, failures persist, acknowledgements clear, timestamps keep first value');
console.log('ALL_CLIENT_QUEUE_FAULT_CHECKS_PASSED');
