# BarSan Experiment — database architecture

## Application shape

The target is one FastAPI/Python application and one PostgreSQL database. It contains three separately versioned modules:

1. `questionnaire` — questionnaire and resumable answer draft.
2. `lifetrade` — the existing DD vs Life-Trade task.
3. `ultimatum` — the 20-round, four-block Ultimatum Game plus practice trials and trial-level audit data.

The existing `/lifetrade` deployment URL is retained while the new participant-facing flow is built. Changing the public URL or deployment mapping is a separate rollout step.

## Shared entities

- `studies`: study/event metadata (configured with `STUDY_CODE` and `STUDY_NAME`).
- `participants`: one row per participant code.
- `study_participations`: joins a participant to the study.
- `experiment_assignments`: one preassigned condition and a hashed, per-person invitation token for a participant in the study. The raw token is returned only when the assignment/link is created.
- `experiment_consents`: consent version and timestamp for an affirmative response; no IP address or device fingerprint is recorded by this consent table.
- `instruments`: registry of modules.
- `instrument_versions`: version and settings metadata for a module.
- `instrument_runs`: one attempt to complete a module, including status, timestamps, attempt-specific metadata and trial count.
- `experiment_stage_progress`: current status for each module in a participant's study participation, including current attempt, current run ID and JSON draft.
- `questionnaire_responses`: one current answer per questionnaire run and hidden question code. Changing an answer upserts that run/question pair, so revisiting a question replaces the value for that question within the same questionnaire attempt.

The existing `lifetrade_trials` table continues to hold Life-Trade trial-level responses. Questionnaire answers are stored in `questionnaire_responses` and keyed by internal question code (for example `B1` or `PJ1`); those codes are not shown in the participant interface. Ultimatum trials are stored separately in `ultimatum_trials`; do not mix them into `lifetrade_trials`.

## Relationship

`participants → study_participations → experiment_assignments`

`study_participations → experiment_stage_progress → instrument_runs → module-specific data`

A participant has one fixed assignment for the study, but can have one current attempt per module. The assignment's condition is resolved server-side from the invitation token and participant code; the browser must not be trusted to provide or change the condition.

## Module order and status

The required module order is:

`questionnaire → dd_lifetrade → ultimatum`

Each module has `not_started`, `in_progress` or `completed` progress status. The first module can be saved as a JSON draft while in progress. On re-entry, the saved draft and the same run ID are returned so the interface can restore answers already entered.

For `dd_lifetrade` and `ultimatum`, starting a module that has an incomplete current attempt marks the old run `superseded` and creates a new attempt from the beginning. Old partial rows are preserved for audit/recovery but are excluded from the primary Life-Trade CSV export. This is preferable to physically deleting trial data during each restart. Only the latest non-superseded attempt is intended for the main analysis export.

A completed module is not restarted when the participant re-enters. The workflow returns its completed status and routes to the next unfinished module. The server enforces the required preceding-module completion when starting a module.

## Per-participant links and assignments

The research team preassigns condition A/B (or a later named condition) before sending links. The admin assignment endpoint accepts participant-code/condition pairs and creates a random invitation token for each person. Only a SHA-256 hash is stored in PostgreSQL. The URL contains the opaque token, not the condition. The participant must also enter the expected participant code; both are checked together.

Do not distribute generated links for live data collection until the full participant journey, including Ultimatum, has passed a pilot on cPanel/PostgreSQL. The current repository includes the new flow, but this is not yet confirmation of deployment.

## Foundational APIs

- `POST /api/admin/assignments`: protected by `X-Admin-Token`; creates fixed assignments and per-participant links.
- `POST /api/experiment/identify`: checks invitation token + entered participant code and returns the condition plus progress for all three modules.
- `POST /api/experiment/stages/start`: starts the first attempt, restores an in-progress questionnaire draft, or supersedes/restarts an incomplete task attempt.
- `POST /api/experiment/stages/draft`: saves questionnaire draft data.
- `POST /api/experiment/questionnaire/answer`: upserts an answer using the hidden question code and updates the resumable questionnaire draft.
- `POST /api/experiment/stages/complete`: marks the current attempt completed; questionnaire completion requires all 25 configured question responses.
- `GET /api/admin/progress`: protected monitoring endpoint with one row per assigned participant and status/attempt numbers for all three stages.
- `GET /api/admin/questionnaire/export.csv`: protected CSV export in long format, one row per completed questionnaire answer.
- `POST /api/experiment/ultimatum/prepare`: records/returns a server-prepared Ultimatum trial and the generated offer when the participant is responder.
- `POST /api/experiment/ultimatum/resolve`: stores the participant's offer or response, server-generated opponent decision, per-trial score, potential points lost, reaction time and waiting duration.
- `GET /api/admin/ultimatum/export.csv`: protected export of all Ultimatum trial rows, including practice and superseded attempts for audit.
- `GET /api/health`: checks service/database connectivity.

The common welcome and ethics/consent pages, questionnaire, DD/Life-Trade module, and initial Ultimatum frontend/backend are present in GitHub. The schema is additive (`CREATE TABLE IF NOT EXISTS`) and does not delete previous trial data. Before production use, the code must pass CI and an end-to-end pilot on the actual cPanel/PostgreSQL deployment; the provisional AI decision parameters and the apparent-coworker deception also require investigator/ethics review.

## PostgreSQL and deployment

PostgreSQL is the intended production database for concurrent participation. SQLite remains supported for local tests. Schema initialization is additive and must not drop existing tables.

Environment variables:

- `DATABASE_URL`: PostgreSQL connection string.
- `STUDY_CODE`: stable study identifier.
- `STUDY_NAME`: study display name.
- `PUBLIC_EXPERIMENT_URL`: base URL used in invitation links (defaults to the current Life-Trade URL during migration).
- `ADMIN_TOKEN`: required for assignment creation, admin listing and CSV export.
- `DB_POOL_MIN_SIZE`, `DB_POOL_MAX_SIZE`, `DB_POOL_TIMEOUT`, `DB_POOL_MAX_WAITING`, `DB_CONNECT_TIMEOUT`: optional connection-pool settings.

Keep database credentials and `ADMIN_TOKEN` out of GitHub and out of browser JavaScript.
