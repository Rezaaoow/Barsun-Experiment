import http from "k6/http";
import { check, sleep } from "k6";
import { Counter, Trend } from "k6/metrics";

const expectedUsers = Number(__ENV.EXPECTED_USERS || 600);
const baseUrl = String(__ENV.BASE_URL || "").replace(/\/$/, "");
const participantsPath = __ENV.PARTICIPANTS_FILE || "./participants.json";
const fixture = JSON.parse(open(participantsPath));
const participants = Array.isArray(fixture) ? fixture : fixture.participants;

if (!baseUrl.startsWith("https://")) {
  throw new Error("BASE_URL must be the HTTPS URL of the dedicated staging app.");
}
if (baseUrl !== "https://staging.my-experiment.ir/lifetrade") {
  throw new Error("This test run is restricted to the verified isolated staging app.");
}
if (!Array.isArray(participants) || participants.length < expectedUsers) {
  throw new Error("The private participant fixture does not contain enough invitations.");
}
if (!Array.isArray(fixture) && fixture.complete !== true) {
  throw new Error("The participant fixture is partial; create a complete cohort first.");
}

const appDuration = new Trend("server_app_ms");
const poolDuration = new Trend("server_pool_ms");
const sqlDuration = new Trend("server_sql_ms");
const outsideDuration = new Trend("outside_app_wait_ms");
let traceSequence = 0;
function traceId() {
  return Date.now().toString(16).padStart(12, "0") + __VU.toString(16).padStart(4, "0") + (++traceSequence).toString(16).padStart(8, "0") + Math.floor(Math.random()*4294967296).toString(16).padStart(8, "0");
}
const completedParticipants = new Counter("completed_participants");
const transientRetryAttempts = new Counter("transient_retry_attempts");
const terminalRequestFailures = new Counter("terminal_request_failures");
const networkTimeouts = new Counter("network_timeouts");
const appPoolOverloads = new Counter("app_pool_overloads");
const webServerOverloads = new Counter("web_server_overloads");
const questionCodes = [
  "B1", "B2", "B3", "B4", "B5",
  "PJ1", "PJ2", "PJ3", "PJ4", "PJ5", "PJ6", "PJ7",
  "DJ1", "DJ2", "DJ3", "DJ4",
  "IJ1", "IJ2", "IJ3", "IJ4",
  "INJ1", "INJ2", "INJ3", "INJ4", "INJ5"
];

const blocks = {
  proposer_agent: "proposer",
  responder_agent: "responder",
  proposer_coworker: "proposer",
  responder_coworker: "responder"
};

const loadProfile = String(__ENV.LOAD_PROFILE || "ramp").toLowerCase();
if (!["ramp", "burst"].includes(loadProfile)) {
  throw new Error("LOAD_PROFILE must be either ramp or burst.");
}

const fullWorkflowScenario = loadProfile === "burst"
  ? {
      executor: "per-vu-iterations",
      vus: expectedUsers,
      iterations: 1,
      maxDuration: "8m",
      gracefulStop: "30s",
      exec: "participantFlow"
    }
  : {
      executor: "ramping-vus",
      startVUs: 0,
      stages: [
        { duration: "20s", target: Math.min(100, expectedUsers) },
        { duration: "20s", target: Math.min(200, expectedUsers) },
        { duration: "20s", target: Math.min(300, expectedUsers) },
        { duration: "20s", target: Math.min(400, expectedUsers) },
        { duration: "20s", target: expectedUsers },
        { duration: "4m", target: expectedUsers },
        { duration: "30s", target: 0 }
      ],
      gracefulRampDown: "30s",
      exec: "participantFlow"
    };

export const options = {
  scenarios: {
    full_participant_workflow: fullWorkflowScenario
  },
  thresholds: {
    http_req_failed: ["rate<0.001"],
    http_req_duration: ["p(95)<500", "p(99)<1500"],
    checks: ["rate>0.999"],
    completed_participants: [`count==${expectedUsers}`]
  }
};
for (const path of ["/api/experiment/questionnaire/answers", "/api/trials", "/api/experiment/ultimatum/prepare", "/api/experiment/ultimatum/resolve", "/api/experiment/ultimatum/events", "/api/experiment/time"]) {
  options.thresholds[`http_req_duration{api_path:${path}}`] = ["p(95)<500", "p(99)<1500"];
}

export function setup() {
  const response = http.get(baseUrl + "/api/health", {
    headers: { "Accept": "application/json" },
    timeout: "15s",
    tags: { api_path: "/api/health-preflight" }
  });
  if (response.status !== 200) {
    throw new Error("Staging health endpoint failed with HTTP " + response.status);
  }
  const runtimeHeader = Object.entries(response.headers).find(([key]) => key.toLowerCase() === "x-barsan-runtime");
  if (!runtimeHeader || runtimeHeader[1] !== "uvicorn-pr25-native") throw new Error("Native ASGI routing is not active.");
  let health;
  try {
    health = response.json();
  } catch (_) {
    throw new Error("Staging health endpoint returned invalid JSON.");
  }
  if (
    health.ok !== true ||
    health.database !== "postgresql" ||
    health.database_connected !== true
  ) {
    throw new Error("Load testing requires a connected PostgreSQL staging database.");
  }
  return {};
}

function post(path, payload, options = {}) {
  // Retries are opt-in and only used for endpoints with idempotent server semantics.
  const maxAttempts = options.retryable === true ? 3 : 1;
  let response = null;
  for (let attempt = 0; attempt < maxAttempts; attempt++) {
    const trace = traceId();
    response = http.post(
      baseUrl + path,
      JSON.stringify(payload),
      {
        headers: {
          "Content-Type": "application/json",
          "X-Barsan-Trace": trace,
          "Accept": "application/json"
        },
        timeout: "45s",
        tags: { api_path: path }
      }
    );
    const timingHeader = Object.entries(response.headers || {}).find(([key]) => key.toLowerCase() === "server-timing");
    const durations = {};
    if (timingHeader) {
      for (const match of String(timingHeader[1]).matchAll(/(app|pool|sql);dur=([0-9.]+)/g)) durations[match[1]] = Number(match[2]);
      if (durations.app !== undefined) {
        appDuration.add(durations.app, {api_path:path});
        poolDuration.add(durations.pool, {api_path:path});
        sqlDuration.add(durations.sql, {api_path:path});
        outsideDuration.add(Math.max(0, response.timings.waiting - durations.app), {api_path:path});
      }
    }
    if (response.status === 0 || response.timings.waiting > 1000) {
      console.log(JSON.stringify({trace, path, status:response.status, waiting_ms:response.timings.waiting, app_ms:durations.app, error_code:response.error_code}));
    }
    if (response.status === 0) networkTimeouts.add(1);
    else if (response.status >= 500) {
      if (String(response.body).includes("Database is temporarily busy")) appPoolOverloads.add(1);
      else webServerOverloads.add(1);
    }
    if (response.status >= 200 && response.status < 300) break;

    const retryableStatus = [0, 408, 425, 429, 500, 502, 503, 504].includes(response.status);
    if (!options.retryable || !retryableStatus || attempt >= maxAttempts - 1) break;

    transientRetryAttempts.add(1, { api_path: path });
    const ceilingMs = Math.min(8000, 500 * Math.pow(2, attempt));
    let delayMs = Math.floor(Math.random() * ceilingMs);
    const retryAfter = response.headers && (
      response.headers["Retry-After"] || response.headers["retry-after"]
    );
    const retryAfterSeconds = Number(retryAfter);
    if (Number.isFinite(retryAfterSeconds) && retryAfterSeconds >= 0) {
      delayMs = Math.max(delayMs, Math.min(60000, retryAfterSeconds * 1000));
    }
    sleep(delayMs / 1000);
  }

  const ok = options.skipCheck === true
    ? response.status >= 200 && response.status < 300
    : check(response, {
        "Final HTTP response returns 2xx": (res) => res.status >= 200 && res.status < 300
      });
  if (!ok) {
    if (options.countTerminalFailure !== false) {
      terminalRequestFailures.add(1, { api_path: path });
    }
    const error = new Error(path + " returned HTTP " + response.status);
    error.status = response.status;
    error.retryable = [0, 408, 425, 429, 500, 502, 503, 504].includes(response.status);
    throw error;
  }
  try {
    return response.json();
  } catch (_) {
    throw new Error(path + " returned invalid JSON");
  }
}

function requestId(prefix) {
  return prefix + "-" + __VU + "-" + Date.now() + "-" + Math.random().toString(16).slice(2);
}

function startStageWithRecovery(payload) {
  return post("/api/experiment/stages/start", {
    ...payload,
    start_request_id: requestId("stage")
  }, { retryable: true });
}


function timestamp() {
  return new Date().toISOString();
}

function startTimeTracking(identity, stageCode, stepId) {
  const payload = { ...identity, stage_code: stageCode, step_id: stepId };
  post("/api/experiment/time", { ...payload, event: "start", event_id: requestId("time") }, { retryable: true });
  let heartbeatIntervalMs = 27000 + Math.floor(Math.random() * 6000);
  let lastHeartbeatAt = Date.now();

  // Match the browser's randomized 27–33 second heartbeat without synchronizing VUs.
  return function heartbeatIfDue() {
    if (Date.now() - lastHeartbeatAt >= heartbeatIntervalMs) {
      post("/api/experiment/time", { ...payload, event: "heartbeat", event_id: requestId("time") }, { retryable: true });
      lastHeartbeatAt = Date.now();
      heartbeatIntervalMs = 27000 + Math.floor(Math.random() * 6000);
    }
  };
}

function completeTimeTracking(identity, stageCode, stepId) {
  post("/api/experiment/time", {
    ...identity,
    stage_code: stageCode,
    step_id: stepId,
    event: "complete",
    event_id: requestId("time")
  }, { retryable: true });
}

function runUltimatumRound(identity, runId, phase, blockCode, blockOrder, trial, sequence, heartbeatIfDue) {
  const prepared = post("/api/experiment/ultimatum/prepare", {
    ...identity,
    run_id: runId,
    phase,
    block_code: blockCode,
    block_order: blockOrder,
    trial_in_block: trial,
    trial_sequence: sequence,
    client_metadata: { source: "k6-staging-load-test" }
  }, { retryable: true });

  const role = phase === "practice"
    ? (blockCode === "practice_proposer_agent" ? "proposer" : "responder")
    : blocks[blockCode];

  // Reproduce the fixed AI wait and randomized coworker wait used by the participant UI.
  const waitSeconds = blockCode.indexOf("agent") >= 0
    ? 2
    : 3 + Math.floor(Math.random() * 3001) / 1000;
  sleep(waitSeconds);
  heartbeatIfDue();

  const resolvePayload = {
    ...identity,
    run_id: runId,
    round_id: prepared.round_id
  };
  if (role === "proposer") {
    resolvePayload.participant_offer_to_opponent = 5;
  } else {
    resolvePayload.participant_decision = "accept";
  }

  const result = post("/api/experiment/ultimatum/resolve", resolvePayload, { retryable: true });

  const events = {
    result_screen_shown_at: timestamp(),
    advance_clicked_at: timestamp()
  };
  if (phase === "main" && trial < 5) {
    events.next_trial_clicked_at = timestamp();
  }
  post("/api/experiment/ultimatum/events", {
    ...identity,
    run_id: runId,
    round_id: result.round_id,
    event_timestamps: events
  }, { retryable: true });

  return result;
}

export function participantFlow() {
  // One unique invitation per VU. Later loop iterations intentionally do no API work.
  if (__ITER > 0) {
    sleep(60);
    return;
  }

  const participant = participants[__VU - 1];
  if (!participant || !participant.participant_code || !participant.invitation_token) {
    throw new Error("Missing invitation fixture for VU " + __VU);
  }

  const identity = {
    participant_code: participant.participant_code,
    invitation_token: participant.invitation_token
  };

  post("/api/experiment/consent", { ...identity, accepted: true }, { retryable: true });
  const identified = post("/api/experiment/identify", identity, { retryable: true });
  if (identified.current_step_id !== "load-questionnaire") {
    throw new Error("Unexpected initial workflow step for VU " + __VU);
  }

  // Module 1: persist all 25 questionnaire answers, then complete the stage.
  const questionnaire = startStageWithRecovery({
    ...identity,
    stage_code: "questionnaire",
    step_id: "load-questionnaire"
  });
  const questionnaireHeartbeat = startTimeTracking(identity, "questionnaire", "load-questionnaire");
  const answers = {};
  let answerBatch = [];
  const questionnaireBatchSize = 5;
  for (let index = 0; index < questionCodes.length; index++) {
    // Synthetic but plausible short reading/decision interval for each question.
    sleep(3);
    const code = questionCodes[index];
    const value = String((index % 5) + 1);
    answers[code] = value;
    answerBatch.push({
      question_code: code,
      response_value: value,
      current_question_index: index
    });
    if (answerBatch.length >= questionnaireBatchSize || index === questionCodes.length - 1) {
      post("/api/experiment/questionnaire/answers", {
        ...identity,
        run_id: questionnaire.run_id,
        step_id: "load-questionnaire",
        answers: answerBatch
      }, { retryable: true });
      answerBatch = [];
      questionnaireHeartbeat();
    }
  }
  post("/api/experiment/stages/complete", {
    ...identity,
    stage_code: "questionnaire",
    step_id: "load-questionnaire",
    run_id: questionnaire.run_id,
    final_payload: {
      schema_version: 1,
      answers,
      question_order: questionCodes
    }
  }, { retryable: true });
  completeTimeTracking(identity, "questionnaire", "load-questionnaire");

  // Module 2: submit 30 synthetic Life-Trade rows in bounded batches.
  const dd = startStageWithRecovery({
    ...identity,
    stage_code: "dd_lifetrade",
    step_id: "load-dd"
  });
  const ddHeartbeat = startTimeTracking(identity, "dd_lifetrade", "load-dd");
  const trialRows = [];
  for (let index = 0; index < 30; index++) {
    trialRows.push({
      client_event_id: "k6-" + dd.run_id + "-" + index,
      row: {
        task: index < 15 ? "LT" : "KT",
        task_order: index < 15 ? 1 : 2,
        block_name: ["Low", "Mid", "High"][Math.floor((index % 15) / 5)],
        magnitude: "1M",
        trial_in_block: (index % 5) + 1,
        index: index + 1,
        user_choice_type: index % 2 === 0 ? "Now" : "Later",
        reaction_time_ms: 500
      }
    });
  }
  // The participant UI normally flushes the local queue every five responses.
  for (let offset = 0; offset < trialRows.length; offset += 5) {
    // Five trial intervals (about 1.5 seconds each) precede each local-queue flush.
    sleep(7.5);
    post("/api/trials", {
      session_id: dd.run_id,
      trials: trialRows.slice(offset, offset + 5)
    }, { retryable: true });
    ddHeartbeat();
  }
  post("/api/complete", {
    session_id: dd.run_id,
    status: "completed",
    trial_count: trialRows.length
  }, { retryable: true });
  post("/api/experiment/stages/complete", {
    ...identity,
    stage_code: "dd_lifetrade",
    step_id: "load-dd",
    run_id: dd.run_id
  }, { retryable: true });
  completeTimeTracking(identity, "dd_lifetrade", "load-dd");

  // Module 3: complete both practice trials and all 20 main trials.
  const ultimatum = startStageWithRecovery({
    ...identity,
    stage_code: "ultimatum",
    step_id: "load-ultimatum"
  });
  const ultimatumHeartbeat = startTimeTracking(identity, "ultimatum", "load-ultimatum");
  const runId = ultimatum.run_id;
  runUltimatumRound(identity, runId, "practice", "practice_proposer_agent", 0, 1, 0, ultimatumHeartbeat);
  runUltimatumRound(identity, runId, "practice", "practice_responder_coworker", 0, 1, 0, ultimatumHeartbeat);

  const blockOrder = ultimatum.draft_payload.block_order;
  if (!Array.isArray(blockOrder) || blockOrder.length !== 4 || new Set(blockOrder).size !== 4) {
    throw new Error("Server did not return a valid randomized Ultimatum block order.");
  }

  const blockSummaries = {};
  let finalResult = null;
  for (let blockIndex = 0; blockIndex < blockOrder.length; blockIndex++) {
    const blockCode = blockOrder[blockIndex];
    const order = blockIndex + 1;
    let blockResult = null;
    for (let trial = 1; trial <= 5; trial++) {
      const sequence = (order - 1) * 5 + trial;
      blockResult = runUltimatumRound(
        identity, runId, "main", blockCode, order, trial, sequence, ultimatumHeartbeat
      );
      finalResult = blockResult;
    }
    blockSummaries[blockCode] = {
      block_order: order,
      points_earned: Number(blockResult.block_score || 0),
      potential_points_lost: Number(blockResult.block_potential_lost || 0)
    };
  }

  post("/api/experiment/stages/complete", {
    ...identity,
    stage_code: "ultimatum",
    step_id: "load-ultimatum",
    run_id: runId,
    final_payload: {
      schema_version: 1,
      condition_code: identified.condition_code,
      feedback_group: participant.variant === "control" ? "Control" : "Frame",
      block_order: blockOrder,
      final_score: Number(finalResult.total_score || 0),
      total_potential_points_lost: Number(finalResult.total_potential_lost || 0),
      block_summaries: blockSummaries,
      practice_excluded_from_score: true,
      client_completed_at: timestamp()
    }
  }, { retryable: true });
  completeTimeTracking(identity, "ultimatum", "load-ultimatum");

  completedParticipants.add(1);
}


