import http from "k6/http";
import { check, fail, sleep } from "k6";
import { SharedArray } from "k6/data";

const BASE_URL = String(__ENV.BASE_URL || "").replace(/\/$/, "");
const PARTICIPANTS_FILE = __ENV.PARTICIPANTS_FILE || "loadtest/participants.json";
const VUS = Number(__ENV.VUS || 600);
const PROFILE = __ENV.PROFILE || "burst";
const THINK_TIME = Math.max(0, Number(__ENV.THINK_TIME_SECONDS || 0.1));
const REQUEST_TIMEOUT = __ENV.REQUEST_TIMEOUT || "30s";

if (!BASE_URL) throw new Error("Set BASE_URL to a staging application URL.");
if (/^https:\/\/(www\.)?my-experiment\.ir(\/|$)/i.test(BASE_URL)) {
  throw new Error("Refusing to run a load test against the production host.");
}
if (!["burst", "ramp"].includes(PROFILE)) throw new Error("PROFILE must be burst or ramp.");

const participants = new SharedArray("staging participants", () => {
  const data = JSON.parse(open(PARTICIPANTS_FILE));
  if (!Array.isArray(data)) throw new Error("Participant file must contain a JSON array.");
  return data;
});
if (participants.length < VUS) {
  throw new Error("Participant file contains fewer invitations than configured VUs.");
}

const QUESTION_CODES = [
  "B1","B2","B3","B4","B5",
  "PJ1","PJ2","PJ3","PJ4","PJ5","PJ6","PJ7",
  "DJ1","DJ2","DJ3","DJ4",
  "IJ1","IJ2","IJ3","IJ4",
  "INJ1","INJ2","INJ3","INJ4","INJ5"
];
const HEADERS = { "Content-Type": "application/json", "Accept": "application/json" };
let timer = null;

export const options = {
  scenarios: {
    full_event: PROFILE === "ramp"
      ? {
          executor: "ramping-vus",
          startVUs: 0,
          stages: [
            { duration: "1m", target: Math.min(150, VUS) },
            { duration: "2m", target: VUS },
            { duration: "10m", target: VUS },
            { duration: "1m", target: 0 }
          ],
          gracefulRampDown: "45s"
        }
      : {
          executor: "per-vu-iterations",
          vus: VUS,
          iterations: 1,
          maxDuration: "30m"
        }
  },
  thresholds: {
    http_req_failed: ["rate<0.001"],
    http_req_duration: ["p(95)<1500", "p(99)<4000"],
    checks: ["rate>0.999"]
  }
};

function requestOk(response, label) {
  const ok = check(response, {
    [label + " returned 2xx"]: (r) => r.status >= 200 && r.status < 300
  });
  if (!ok) fail(label + " failed with HTTP " + response.status);
}

function post(path, payload, label) {
  const response = http.post(
    BASE_URL + path,
    JSON.stringify(payload),
    { headers: HEADERS, timeout: REQUEST_TIMEOUT, tags: { name: label } }
  );
  requestOk(response, label);
  try {
    return response.json();
  } catch (_) {
    fail(label + " returned invalid JSON");
  }
}

function get(path, label) {
  const response = http.get(
    BASE_URL + path,
    { headers: { Accept: "application/json" }, timeout: REQUEST_TIMEOUT, tags: { name: label } }
  );
  requestOk(response, label);
  try {
    return response.json();
  } catch (_) {
    fail(label + " returned invalid JSON");
  }
}

function isoNow() {
  return new Date().toISOString();
}

function startTimer(participant, step, stage) {
  timer = {
    participant,
    stepId: step.step_id,
    stageCode: stage,
    lastHeartbeat: Date.now()
  };
  post("/api/experiment/time", {
    invitation_token: participant.invitation_token,
    participant_code: participant.participant_code,
    step_id: step.step_id,
    stage_code: stage,
    event: "start"
  }, "time-start");
}

function maybeHeartbeat() {
  if (!timer || Date.now() - timer.lastHeartbeat < 15000) return;
  post("/api/experiment/time", {
    invitation_token: timer.participant.invitation_token,
    participant_code: timer.participant.participant_code,
    step_id: timer.stepId,
    stage_code: timer.stageCode,
    event: "heartbeat"
  }, "time-heartbeat");
  timer.lastHeartbeat = Date.now();
}

function think() {
  if (THINK_TIME > 0) sleep(THINK_TIME);
  maybeHeartbeat();
}

function finishTimer() {
  if (!timer) return;
  post("/api/experiment/time", {
    invitation_token: timer.participant.invitation_token,
    participant_code: timer.participant.participant_code,
    step_id: timer.stepId,
    stage_code: timer.stageCode,
    event: "complete"
  }, "time-complete");
  timer = null;
}

function identityPayload(participant) {
  return {
    invitation_token: participant.invitation_token,
    participant_code: participant.participant_code
  };
}

function runQuestionnaire(participant, step) {
  const started = post("/api/experiment/stages/start", {
    ...identityPayload(participant),
    stage_code: "questionnaire",
    step_id: step.step_id
  }, "questionnaire-start");
  if (started.already_completed) {
    finishTimer();
    return;
  }

  const answers = {};
  for (let index = 0; index < QUESTION_CODES.length; index++) {
    const code = QUESTION_CODES[index];
    const answer = String(((index + __VU) % 5) + 1);
    answers[code] = answer;
    post("/api/experiment/questionnaire/answer", {
      ...identityPayload(participant),
      run_id: started.run_id,
      question_code: code,
      response_value: answer,
      current_question_index: index
    }, "questionnaire-answer");
    post("/api/experiment/stages/draft", {
      ...identityPayload(participant),
      stage_code: "questionnaire",
      step_id: step.step_id,
      run_id: started.run_id,
      draft_payload: { schema_version: 1, answers, current_question_index: index + 1 }
    }, "questionnaire-draft");
    think();
  }

  post("/api/experiment/stages/complete", {
    ...identityPayload(participant),
    stage_code: "questionnaire",
    step_id: step.step_id,
    run_id: started.run_id,
    final_payload: {
      schema_version: 1,
      answers,
      question_order: QUESTION_CODES
    }
  }, "questionnaire-complete");
  finishTimer();
}

function runLifeTrade(participant, step) {
  const started = post("/api/experiment/stages/start", {
    ...identityPayload(participant),
    stage_code: "dd_lifetrade",
    step_id: step.step_id
  }, "lifetrade-start");
  if (started.already_completed) {
    finishTimer();
    return;
  }

  const runId = started.run_id;
  const records = [];
  for (let index = 0; index < 60; index++) {
    const taskIndex = index < 30 ? 0 : 1;
    records.push({
      client_event_id: participant.participant_code + "-LT-" + String(index + 1),
      row: {
        task: taskIndex === 0 ? "KT" : "LT",
        task_order: taskIndex + 1,
        block_name: "load-block-" + String(Math.floor((index % 30) / 5) + 1),
        magnitude: ["small", "medium", "large"][index % 3],
        trial_in_block: (index % 5) + 1,
        index: index + 1,
        user_choice_type: index % 2 === 0 ? "Now" : "Later",
        reaction_time_ms: 350 + ((index + __VU) % 5) * 25,
        load_test_synthetic: true
      }
    });
  }

  for (let offset = 0; offset < records.length; offset += 25) {
    post("/api/trials", {
      session_id: runId,
      trials: records.slice(offset, offset + 25)
    }, "lifetrade-batch");
    think();
  }

  post("/api/complete", {
    session_id: runId,
    status: "completed",
    trial_count: records.length
  }, "lifetrade-run-complete");
  post("/api/experiment/stages/complete", {
    ...identityPayload(participant),
    stage_code: "dd_lifetrade",
    step_id: step.step_id,
    run_id: runId
  }, "lifetrade-stage-complete");
  finishTimer();
}

function prepareUltimatumRound(participant, runId, phase, blockCode, blockOrder, trial, sequence) {
  return post("/api/experiment/ultimatum/prepare", {
    ...identityPayload(participant),
    run_id: runId,
    phase,
    block_code: blockCode,
    block_order: blockOrder,
    trial_in_block: trial,
    trial_sequence: sequence,
    client_metadata: { load_test_synthetic: true, screen_width: 390, screen_height: 844 }
  }, "ultimatum-prepare");
}

function resolveUltimatumRound(participant, runId, round, phase, blockCode, trial) {
  const timestamp = isoNow();
  const isProposer = blockCode.indexOf("proposer") >= 0;
  const payload = {
    ...identityPayload(participant),
    run_id: runId,
    round_id: round.round_id,
    decision_screen_shown_at: timestamp,
    responded_at: timestamp,
    reaction_time_ms: 650 + (__VU % 200),
    wait_duration_ms: blockCode.indexOf("coworker") >= 0 ? 4000 : 2000,
    client_metadata: { load_test_synthetic: true },
    client_event_timestamps: {}
  };
  if (isProposer) payload.participant_offer_to_opponent = 5;
  else payload.participant_decision = "accept";

  const result = post("/api/experiment/ultimatum/resolve", payload, "ultimatum-resolve");
  const advanceAt = isoNow();
  const eventTimestamps = {
    result_screen_shown_at: advanceAt,
    advance_clicked_at: advanceAt
  };
  if (phase === "main" && trial < 5) eventTimestamps.next_trial_clicked_at = advanceAt;
  post("/api/experiment/ultimatum/events", {
    ...identityPayload(participant),
    run_id: runId,
    round_id: round.round_id,
    event_timestamps: eventTimestamps
  }, "ultimatum-events");
  think();
  return result;
}

function runUltimatum(participant, step, identity) {
  const started = post("/api/experiment/stages/start", {
    ...identityPayload(participant),
    stage_code: "ultimatum",
    step_id: step.step_id
  }, "ultimatum-start");
  if (started.already_completed) {
    finishTimer();
    return;
  }
  const runId = started.run_id;
  const blockOrder = started.draft_payload && started.draft_payload.block_order;
  if (!Array.isArray(blockOrder) || blockOrder.length !== 4) {
    fail("Ultimatum start did not return the server-assigned block order.");
  }

  const practiceProposer = prepareUltimatumRound(
    participant, runId, "practice", "practice_proposer_agent", 0, 1, 0
  );
  resolveUltimatumRound(
    participant, runId, practiceProposer, "practice", "practice_proposer_agent", 1
  );
  const practiceResponder = prepareUltimatumRound(
    participant, runId, "practice", "practice_responder_coworker", 0, 1, 0
  );
  resolveUltimatumRound(
    participant, runId, practiceResponder, "practice", "practice_responder_coworker", 1
  );

  const blockSummaries = {};
  let lastResult = null;
  for (let blockIndex = 0; blockIndex < blockOrder.length; blockIndex++) {
    const blockCode = blockOrder[blockIndex];
    const blockOrderNumber = blockIndex + 1;
    for (let trial = 1; trial <= 5; trial++) {
      const sequence = (blockIndex * 5) + trial;
      const round = prepareUltimatumRound(
        participant, runId, "main", blockCode, blockOrderNumber, trial, sequence
      );
      lastResult = resolveUltimatumRound(participant, runId, round, "main", blockCode, trial);
      if (trial === 5) {
        blockSummaries[blockCode] = {
          block_order: blockOrderNumber,
          points_earned: Number(lastResult.block_score || 0),
          potential_points_lost: Number(lastResult.block_potential_lost || 0)
        };
      }
    }
  }

  post("/api/experiment/stages/complete", {
    ...identityPayload(participant),
    stage_code: "ultimatum",
    step_id: step.step_id,
    run_id: runId,
    final_payload: {
      schema_version: 1,
      condition_code: identity.condition_code,
      feedback_group: identity.condition_code === "A" ? "Frame" : "Control",
      block_order: blockOrder,
      final_score: Number(lastResult.total_score || 0),
      total_potential_points_lost: Number(lastResult.total_potential_lost || 0),
      block_summaries: blockSummaries,
      practice_excluded_from_score: true,
      client_completed_at: isoNow()
    }
  }, "ultimatum-stage-complete");
  finishTimer();
}

export default function () {
  const participant = participants[__VU - 1];
  if (!participant || !participant.invitation_token || !participant.participant_code) {
    fail("Missing participant invitation for VU " + __VU);
  }

  get(
    "/api/experiment/workflow?invite=" + encodeURIComponent(participant.invitation_token),
    "workflow-outline"
  );
  post("/api/experiment/consent", {
    ...identityPayload(participant),
    accepted: true
  }, "participant-consent");
  const identity = post("/api/experiment/identify", identityPayload(participant), "participant-identify");

  const steps = Array.isArray(identity.workflow_steps) ? identity.workflow_steps : [];
  if (steps.length !== 3) fail("Load-test assignments must have the configured three-step workflow.");
  for (const step of steps) {
    const moduleCode = step.module_code;
    const stage = moduleCode === "ultimatum_frame" || moduleCode === "ultimatum_control"
      ? "ultimatum"
      : moduleCode;
    startTimer(participant, step, stage);
    if (stage === "questionnaire") runQuestionnaire(participant, step);
    else if (stage === "dd_lifetrade") runLifeTrade(participant, step);
    else if (stage === "ultimatum") runUltimatum(participant, step, identity);
    else fail("Unsupported stage in load-test workflow: " + moduleCode);
  }

  if (PROFILE === "ramp") sleep(900);
}
