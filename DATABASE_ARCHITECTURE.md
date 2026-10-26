# BarSan Experiment — database architecture

## Application shape

The target is one FastAPI/Python application and one PostgreSQL database. It contains three separately versioned modules:

1. `questionnaire` — questionnaire and resumable answer draft.
2. `lifetrade` — the existing DD vs Life-Trade task.
3. `ultimatum` — the planned Ultimatum Game.

The existing `/lifetrade` deployment URL is retained while the new participant-facing flow is built. Changing the public URL or deployment mapping is a separate rollout step.

## Shared entities

- `studies`: study/event metadata (configured with `STUDY_CODE` and `STUDY_NAME`).
- `participants`: one row per participant code.
- `study_participations`: joins a participant to the study.
- `experiment_assignments`: one preassigned condition and a hashed, per-person invitation token for a participant in the study. The raw token is returned only when the assignment/link is created.
- `instruments`: registry of modules.
- `instrument_versions`: version and settings metadata for a module.
- `instrument_runs`: one attempt to complete a module, including status, timestamps, attempt-specific metadata and trial count.
- `experiment_stage_progress`: current status for each module in a participant's study participation, including current attempt, current run ID and JSON draft.

The existing `lifetrade_trials` table continues to hold Life-Trade trial-level responses. A separate questionnaire response format and an `ultimatum_rounds` result table will be finalized after the questionnaire and Ultimatum protocols are specified; do not store Ultimatum data in `lifetrade_trials`.

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

Do not distribute the generated links until the participant-facing HTML flow has been connected to the new identity/progress endpoints and end-to-end tested. During the migration, the old Life-Trade-only start flow is still present and does not yet use invitation tokens.

## Foundational APIs

- `POST /api/admin/assignments`: protected by `X-Admin-Token`; creates fixed assignments and per-participant links.
- `POST /api/experiment/identify`: checks invitation token + entered participant code and returns the condition plus progress for all three modules.
- `POST /api/experiment/stages/start`: starts the first attempt, restores an in-progress questionnaire draft, or supersedes/restarts an incomplete task attempt.
- `POST /api/experiment/stages/draft`: saves questionnaire draft data.
- `POST /api/experiment/stages/complete`: marks the current attempt completed; questionnaire completion requires its final payload.
- `GET /api/health`: checks service/database connectivity.

The module UI and protocol-specific validation are intentionally being added in stages. Before production use, connect the HTML to these endpoints, implement the actual consent/transparency and questionnaire screens, define the Ultimatum round data model, and test interruption/re-entry using the production deployment.

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
