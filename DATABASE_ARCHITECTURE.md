# LifeTrade database architecture

## Design intent

The database is shared by the overall study, but data for each instrument is stored separately. A participant enters an existing participant code at the start of the study. The API normalizes the value by trimming surrounding whitespace and converting letters to uppercase; it does not generate a participant-facing code.

The internal participant_id, participation_id, run_id, and instrument_version_id values are database identifiers, not codes that participants are asked to use.

## Current LifeTrade tables

- studies: study/event metadata (configured with STUDY_CODE and STUDY_NAME).
- participants: one row for each entered participant code.
- study_participations: links a participant to a particular study. The unique constraint prevents multiple participation records for the same participant and study.
- instruments: registry of instruments. The initial entry is lifetrade.
- instrument_versions: records the version string sent by the browser and its configuration metadata.
- instrument_runs: one execution of one instrument version by a study participant, including timestamps, status and trial count.
- lifetrade_trials: only LifeTrade trial-level responses. Each event has a unique client_event_id; the complete original response object is retained in payload, and common analysis fields are also stored in typed columns.

The main relationship is:

participants → study_participations → instrument_runs → lifetrade_trials

An instrument run references one instrument_version, which references instruments. A second instrument can use the same study_participations record while writing results to its own run and result tables.

## Add future instruments

When the second task is ready:

1. Add an instruments registry entry and version.
2. Create a task-specific run/result table (for example, ultimatum_rounds) with a foreign key to instrument_runs.run_id, or introduce a separate instrument-run subtype if the task needs distinct run metadata.
3. Keep one shared participant code and one study_participations record; do not create a new participant for each task.
4. For a questionnaire, use versioned questionnaire definitions and a separate response table keyed to its instrument run. Do not put questionnaire fields into lifetrade_trials.

This keeps the current LifeTrade data isolated while allowing cross-instrument analysis by joining through participation and participant records.

## Participant code behavior

The first use of an entered code creates a participant record if that code is not already present; subsequent instruments using the same normalized code resolve to the same participant. No participant-facing code is generated. This does mean a mistyped code would be treated as a different code. If the study has a prepared approved list of codes, load that list into participants before collection and add a validation-only check so unknown codes are rejected.

## Database choice and scaling

PostgreSQL is the intended production database for a synchronized event with many participants. The app uses a bounded per-process connection pool and bulk-inserts up to 25 trials per request. Remember the total possible database connections are approximately:

DB_POOL_MAX_SIZE × number of application processes

The defaults are conservative for shared hosting. Do not increase the pool without checking the provider's connection limit and memory limits.

SQLite remains supported for local development and testing, with WAL and a busy timeout; it is not the preferred production store for many simultaneous writers.

## Environment variables

Set these in the hosting panel; never commit credentials to GitHub:

- DATABASE_URL: PostgreSQL connection string provided by the host.
- STUDY_CODE: stable short code for this event/study (default main-study).
- STUDY_NAME: display name for this study (default Main behavioral study).
- DB_POOL_MIN_SIZE: optional, default 1.
- DB_POOL_MAX_SIZE: optional, default 4.
- DB_POOL_TIMEOUT: optional, default 5 seconds.
- DB_POOL_MAX_WAITING: optional, default 128.
- DB_CONNECT_TIMEOUT: optional, default 5 seconds.
- ADMIN_TOKEN: required for the admin listing and CSV export endpoints.

On startup, the application creates any missing schema objects and registers the study and LifeTrade instrument idempotently. It does not drop tables. Existing legacy SQLite sessions and trials tables are left intact; their data is not automatically copied into the normalized tables.
