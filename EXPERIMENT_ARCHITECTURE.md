# BarSan Experiment — workflow architecture

## Scope and current implementation status

The repo contains the shared participant, assignment, consent and stage-resume foundation; the welcome/ethics pages, 25-item questionnaire, DD/Life-Trade task, and an initial Ultimatum UI/API/data model are implemented in GitHub. The AI response parameters remain provisional, and the code has not been deployed to or end-to-end tested on cPanel. This document describes the repository state, not the hosted site.

## Participant journey

1. Participant opens their unique invitation URL and enters their colleague/participant code.
2. The server validates both values together and retrieves the preassigned experimental condition. The condition is immutable during the study.
3. Participant reads the study explanation/transparency and records affirmative consent (currently version `barsan-consent-v1`).
4. Module 1: questionnaire.
5. Module 2: existing DD vs Life-Trade task.
6. Module 3: Ultimatum Game.
7. Completion screen.

The explanation/consent preamble is before the three research modules. Module order is fixed. The two conditions are assigned outside the app in advance; each participant receives their own unique link. The app must not randomize participants between A and B.

## Refresh, close, interruption and re-entry

Progress is server-side. Local browser storage may be used as a retry queue for unsent events, but must not be the source of truth for completed modules.

| Situation | Required behavior |
|---|---|
| Participant returns and enters the same code through their original link | Server validates identity and returns module progress |
| Questionnaire is incomplete | Restore the same questionnaire run and its most recently autosaved answers |
| Questionnaire is complete | Do not show it again; route to the next unfinished module |
| DD vs Life-Trade is incomplete | On re-entry, supersede the previous attempt and create a clean attempt from the first trial |
| Ultimatum is incomplete | On re-entry, supersede the previous attempt and create a clean attempt from the first round |
| DD/Life-Trade is complete | Preserve it and do not restart it |
| Ultimatum is complete | Preserve it and show the final completion state |

For incomplete task attempts, the prior rows are retained with a `superseded` run status for traceability. They must not appear in the primary analysis export. The new attempt receives a new run ID and starts with an empty task state. This avoids accidentally pooling a partial attempt with the restarted attempt while retaining an audit trail.

Questionnaire draft answers are saved to `experiment_stage_progress.draft_payload` as the initial generic mechanism. Once the questionnaire specification is confirmed, its schema and per-question validation can be refined; each page/answer must autosave and completion must be explicit.

## Condition assignment and links

- Each participant code has at most one assignment per study.
- Each assignment is paired with a condition code and a cryptographically random invitation token.
- PostgreSQL stores only the SHA-256 hash of the token.
- The invitation URL contains an opaque `invite` token; the condition is stored on the server and is never accepted as authority from a URL query parameter.
- A request is accepted only when the link token and participant code match the same assignment.
- Re-entry through the same link retains the original condition.
- Assignment creation requires `X-Admin-Token`; never put this token in the browser or distribute it to participants.

Do not distribute invitation URLs for final data collection yet. The UI includes the welcome/ethics pages, one-question-per-page questionnaire, DD/Life-Trade task and initial Ultimatum flow. Its cPanel deployment and full hosted database path have not been verified; the AI decision model still needs researcher confirmation.

## Experiment-wide admin dashboard

- `GET /admin` and `GET /admin/links` serve the same Persian right-to-left dashboard. Dashboard actions still require `X-Admin-Token`; the token is not persisted in local storage.
- Participant assignments can be created by pasting lines or uploading a CSV with `participant_code`/`code` and `group`/`condition_code` columns. Frame maps to `A`; Control maps to `B`. Raw invitation tokens are returned once at creation and only their SHA-256 hashes are stored in the database.
- `POST /api/admin/test-links` creates a pair of full Experiment test links, one Frame and one Control, both using code `TEST`; each generation has two separate `barsan-test-...` study codes.
- `POST /api/admin/module-test-links` creates a single-module test link (questionnaire, DD/Life-Trade, or Ultimatum) with code `TEST`. When prior stage completion is needed to reach the selected module, synthetic completed prerequisite runs and stage states are inserted with `test_prerequisite=true`; they contain no observed participant answers and must not be treated as behavioral data. The selected condition is returned as `A` or `B`.
- `GET /api/admin/progress?scope=main|test` returns each active assignment, per-module status and attempts, current stage, completion count, last activity, and `progress_state`. `stale_incomplete` is an operational label for more than 24 hours since recorded activity, not conclusive proof that a participant abandoned the experiment.
- Individual module CSV exports support `?scope=test`. `GET /api/admin/export-all.zip?scope=main|test` packages the questionnaire, Life-Trade and Ultimatum CSVs plus participant progress into one ZIP. Without `scope=test`, exports use only the configured main `STUDY_CODE`.
- `GET /api/admin/maintenance/preview?scope=main|test` counts the selected scoped rows before deletion. `POST /api/admin/maintenance/reset` requires the exact matching phrase (`CLEAR MAIN DATA` or `CLEAR TEST DATA`) and deletes only within that study scope, child rows first. Instrument definitions and the global `participants` master rows are retained. Main reset invalidates the original main assignments; test reset removes all `barsan-test-*` study scopes.
- The admin dashboard offers a destructive action only after a preview and exact phrase plus browser confirmation. Nevertheless, take an independent PostgreSQL backup before resetting live data.
- `PUBLIC_EXPERIMENT_URL` overrides invitation URL generation. If it is blank, the app derives the base path from the incoming request and ASGI `root_path`; when deploying to cPanel under a sub-URI, set the explicit full public URL for reliable links.

## Test data isolation details

Test studies share the global participant-code registry intentionally: the two full-test links can use code `TEST` in distinct studies without sharing stage progress. All test assignments, participation records, stage progress, module runs and result rows remain scoped through `study_code`. The three module exports and the combined ZIP apply the scope filter at query time; a test export cannot include main-study rows and vice versa.

## Backend foundation implemented

- Three instrument registry entries: `questionnaire`, `lifetrade`, `ultimatum`.
- `experiment_assignments`: immutable participant-to-condition mapping plus hashed token.
- `experiment_stage_progress`: current module status, attempt number, active run ID and questionnaire draft.
- `questionnaire_responses`: one answer per hidden question code and questionnaire attempt, with answer replacement/upsert when a participant changes a response.
- Identity/progress endpoint.
- Module start endpoint with fixed order enforcement and distinct resume/restart behavior.
- Server-enforced consent recording, versioned by `barsan-consent-v1`.
- Questionnaire draft save, per-question answer upsert and validated stage completion endpoints.
- Protected module-progress monitoring and questionnaire CSV export endpoints.
- Ultimatum table `ultimatum_trials`, server-side offer/decision generation, server-randomized block order, strict practice/block/trial sequencing, scoring, attempt-level audit metadata and protected trial CSV export.
- Structured Ultimatum client interaction timestamps and server-side opponent-generation timestamps are persisted in the existing trial JSON payload (no destructive schema migration); the protected CSV export includes both a complete JSON column and one flat column per timestamp for analysis.
- Protected assignment creation endpoint.
- Primary Life-Trade CSV export excludes runs marked `superseded`.

## APIs

`POST /api/admin/assignments`

Header: `X-Admin-Token: <ADMIN_TOKEN>`

Body:
```json
{
  "assignments": [
    {"participant_code": "BS-0001", "condition_code": "A"},
    {"participant_code": "BS-0002", "condition_code": "B"}
  ]
}
```

The response includes each generated invitation URL once. Keep the response private and securely distribute each participant's own URL.

`POST /api/experiment/consent`

Body: `invitation_token`, `participant_code`, `accepted: true`. Stores the current consent version and acceptance timestamp; the API refuses access when consent has not been accepted.

`POST /api/experiment/identify`

Body: `invitation_token`, `participant_code`. Returns the server-side condition and stage status, and requires recorded consent.

`POST /api/experiment/stages/start`

Body: `invitation_token`, `participant_code`, `stage_code`. Enforces order. An in-progress questionnaire resumes the same run; an incomplete DD/Life-Trade or Ultimatum start creates a new attempt and marks the prior attempt superseded.

`POST /api/experiment/stages/draft`

Body: `invitation_token`, `participant_code`, `run_id`, `draft_payload`. Saves an in-progress questionnaire draft only for the active questionnaire run.

`POST /api/experiment/ultimatum/prepare`

Body: `invitation_token`, `participant_code`, `run_id`, `phase`, `block_code`, `block_order`, `trial_in_block`, `trial_sequence`. Creates or resumes a prepared trial row and returns the server-generated opponent offer when the participant is in the responder role.

`POST /api/experiment/ultimatum/resolve`

Body: invitation/code/run/round IDs, either the participant's offer to the opponent (proposer role) or `accept`/`reject` (responder role), plus timing, client metadata, and allowlisted event timestamps such as proposer-screen display, first/last slider change, offer submission, waiting start/end, responder offer display, and participant choice. The server resolves the opponent decision for proposer trials and persists points earned/lost, client event timestamps, and server-side opponent-generation timestamps.

`POST /api/experiment/ultimatum/events`

Body: invitation/code/run/round IDs and allowlisted post-resolution timestamps such as result-screen display and the continue/next-trial click. These events can only be attached to a completed trial in the participant's active attempt. Retries preserve the first timestamp for each event name.

`GET /api/admin/ultimatum/export.csv`

Protected by `X-Admin-Token`. Exports each Ultimatum trial, practice rows, attempt metadata, timing fields and model/debug payload for analysis and audit.

`POST /api/experiment/stages/complete`

Body: `invitation_token`, `participant_code`, `stage_code`, `run_id`; questionnaire also requires `final_payload`, Ultimatum requires the final score, block order and potential-points-lost total. Rejects completion from stale/superseded run IDs and checks all 20 main trials are stored.

## Ultimatum implementation assumptions (not yet approved for live collection)

- Four main blocks: proposer/agent, responder/agent, proposer/coworker, responder/coworker. Every participant completes all four blocks, five trials per block (20 main trials total). The server creates and stores a random block order in both `experiment_stage_progress.draft_payload` and `instrument_runs.metadata`; APIs reject a different order, skipped trials, attempts to start the main phase before both practice trials are complete, and attempts to start the responder practice before the proposer practice is complete.
- The UI currently conducts two separate practice trials, one per role, and excludes them from scoring. The brief initially says one practice round but later describes a practice flow for both roles; confirm this before final lock.
- Condition codes `A` and `FRAME`/`FRAMING` are currently treated as Frame, with other values shown as Control. Confirm `A=Frame`, `B=Control` before use.
- The AI proposer uses a discretized normal distribution over integer self-shares 0–10, mean 5 and SD 2, truncated to that support. The AI responder currently uses a provisional binomial model (`n=10`, `p=offered share to AI/10`, accept when the draw is at least 5). Every trial stores the model name, distribution parameters, random draw and decision in its payload; the CSV export also exposes these in separate columns. This is an operational placeholder, not a validated decision rule; obtain the investigator's final parameters.
- In coworker blocks the interface displays a coworker opponent, while the actual choices are still generated by the computer. This is experimental deception and needs appropriate ethics approval and a planned debrief consistent with the approved protocol.

## Pre-launch checklist

- Finalize explanation/transparency and consent text.
- Confirm group mapping and whether the practice phase needs one or two trials.
- Confirm AI responder probability rule, binomial parameters and thresholds, and normal distribution mean/SD/truncation.
- Ensure the opponent-display manipulation and debrief are covered by the approved ethics protocol.
- Test refresh at every stage, browser close/reopen, dropped network, duplicate submission, wrong code/link combination, fixed condition, all 22 Ultimatum rows, and complete-vs-incomplete stage routing on the hosted PostgreSQL database.
- Deploy only after automated tests, a pilot on the actual cPanel/PostgreSQL database, and review of data exports.
