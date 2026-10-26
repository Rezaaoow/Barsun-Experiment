# BarSan Experiment — workflow architecture

## Scope of this phase

Prepare the shared participant, assignment, stage-status and resume foundation without implementing the questionnaire items or the Ultimatum Game protocol yet. The currently deployed participant-facing HTML remains the legacy Life-Trade flow until the end-to-end new flow is connected and tested.

## Participant journey

1. Participant opens their unique invitation URL and enters their colleague/participant code.
2. The server validates both values together and retrieves the preassigned experimental condition. The condition is immutable during the study.
3. Participant reads the study explanation/transparency and consent information.
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

Do not distribute invitation URLs yet. The UI still needs to read the invite token, call the identity/progress endpoint, and route participants through all modules. The new backend foundation is prepared, but the old frontend has not yet been switched to this workflow.

## Backend foundation implemented

- Three instrument registry entries: `questionnaire`, `lifetrade`, `ultimatum`.
- `experiment_assignments`: immutable participant-to-condition mapping plus hashed token.
- `experiment_stage_progress`: current module status, attempt number, active run ID and questionnaire draft.
- Identity/progress endpoint.
- Module start endpoint with fixed order enforcement and distinct resume/restart behavior.
- Questionnaire draft save and stage completion endpoints.
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

`POST /api/experiment/identify`

Body: `invitation_token`, `participant_code`. Returns the server-side condition and status of questionnaire, DD/Life-Trade and Ultimatum.

`POST /api/experiment/stages/start`

Body: `invitation_token`, `participant_code`, `stage_code`. Enforces order. An in-progress questionnaire resumes the same run; an incomplete DD/Life-Trade or Ultimatum start creates a new attempt and marks the prior attempt superseded.

`POST /api/experiment/stages/draft`

Body: `invitation_token`, `participant_code`, `run_id`, `draft_payload`. Saves an in-progress questionnaire draft only for the active questionnaire run.

`POST /api/experiment/stages/complete`

Body: `invitation_token`, `participant_code`, `stage_code`, `run_id`; questionnaire also requires `final_payload`. Rejects completion from stale/superseded run IDs.

## Pre-launch checklist

- Connect the invitation landing screen to the new APIs.
- Finalize explanation/transparency and consent text.
- Finalize questionnaire items, answer types, required fields, autosave behavior and completion condition.
- Preserve current Life-Trade task logic while moving it into module 2.
- Specify Ultimatum roles, rounds, condition difference, opponent behavior, scoring and restart behavior.
- Add module-specific response tables and exports where appropriate.
- Test refresh at every stage, browser close/reopen, dropped network, duplicate submission, wrong code/link combination, fixed condition, and complete-vs-incomplete stage routing on the hosted PostgreSQL database.
