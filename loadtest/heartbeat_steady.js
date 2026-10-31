import http from "k6/http";
import { check, fail, sleep } from "k6";
import { SharedArray } from "k6/data";

const BASE_URL = String(__ENV.BASE_URL || "").replace(/\/$/, "");
const PARTICIPANTS_FILE = __ENV.PARTICIPANTS_FILE || "loadtest/participants.json";
const VUS = Number(__ENV.VUS || 600);
const DURATION_SECONDS = Number(__ENV.DURATION_SECONDS || 600);
const HEARTBEAT_SECONDS = 15;

if (!BASE_URL) throw new Error("Set BASE_URL to a staging application URL.");
if (/^https:\/\/(www\.)?my-experiment\.ir(\/|$)/i.test(BASE_URL)) {
  throw new Error("Refusing to run a load test against the production host.");
}

const participants = new SharedArray("staging participants", () => {
  const data = JSON.parse(open(PARTICIPANTS_FILE));
  if (!Array.isArray(data)) throw new Error("Participant file must contain a JSON array.");
  return data;
});
if (participants.length < VUS) throw new Error("Participant file contains fewer invitations than configured VUs.");

export const options = {
  scenarios: {
    steady_600_heartbeats: {
      executor: "per-vu-iterations",
      vus: VUS,
      iterations: 1,
      maxDuration: String(DURATION_SECONDS + 120) + "s"
    }
  },
  thresholds: {
    http_req_failed: ["rate<0.001"],
    http_req_duration: ["p(95)<1000", "p(99)<2500"],
    checks: ["rate>0.999"]
  }
};

function post(payload, label) {
  const response = http.post(
    BASE_URL + "/api/experiment/time",
    JSON.stringify(payload),
    { headers: { "Content-Type": "application/json" }, timeout: "30s", tags: { name: label } }
  );
  const ok = check(response, {
    [label + " returned 2xx"]: (r) => r.status >= 200 && r.status < 300
  });
  if (!ok) fail(label + " failed with HTTP " + response.status);
}

export default function () {
  const participant = participants[__VU - 1];
  if (!participant || !participant.invitation_token || !participant.participant_code) {
    fail("Missing participant invitation for VU " + __VU);
  }
  const identity = {
    invitation_token: participant.invitation_token,
    participant_code: participant.participant_code
  };

  const consent = http.post(
    BASE_URL + "/api/experiment/consent",
    JSON.stringify({ ...identity, accepted: true }),
    { headers: { "Content-Type": "application/json" }, timeout: "30s", tags: { name: "participant-consent" } }
  );
  if (!check(consent, { "consent saved": (r) => r.status === 200 })) {
    fail("Consent failed for heartbeat test.");
  }

  const timerPayload = {
    ...identity,
    step_id: "load-questionnaire",
    stage_code: "questionnaire"
  };
  post({ ...timerPayload, event: "start" }, "time-start");

  const endAt = Date.now() + DURATION_SECONDS * 1000;
  let nextHeartbeat = Date.now() + Math.random() * HEARTBEAT_SECONDS * 1000;
  while (Date.now() < endAt) {
    sleep(Math.max(0.01, (nextHeartbeat - Date.now()) / 1000));
    if (Date.now() < endAt) post({ ...timerPayload, event: "heartbeat" }, "time-heartbeat");
    nextHeartbeat += HEARTBEAT_SECONDS * 1000;
  }

  post({ ...timerPayload, event: "pause" }, "time-pause");
}
