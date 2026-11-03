# AI Agent Instructions — BarSan Experiment

## Current authorized release work — 2026-11-03

The user authorized final Staging fault/stability acceptance followed by Production promotion of the identical tested runtime. See `docs/FINAL_RELEASE_FA.md`. New runtime files include `client_timing.py` and `app/task_timing.js`; include them in pinned manifests. Native launchers will use immutable `deployments/final-<sha>/runtime` sources; `/repo` is mirrored after verification for legacy compatibility. Production remains unchanged until Staging acceptance is complete. Separate Production env/database/invitation key must be preserved. Never run load/fault tests on Production; only release verification. Two synthetic browser invitation fixtures are specifically authorized for the private Mac directory `/Users/rezaaoow/barsan-loadtest/final-release-20261103/`; no other fixture export is authorized by that approval. Actual release results supersede this in-progress note when recorded.


## Last verified staging runtime — 2026-11-03

UT/DD resilience source `68d22169d8d599d4fe6bc2034873c6a58ab7f593` is deployed and verified on Staging. Runtime dependencies now include `task_records.py`, `app/persistence.js` and the versioned local font/license; the launcher pins the whole runtime set. A fresh guarded HTTPS API sequence completed 100/100, 300/300 and 600/600 UT/DD-only workflows with zero HTTP failures/retries, exact PostgreSQL counts and p95 about 11/13/211 ms respectively. The 600 cohort stored 18,000 DD and 13,200 Ultimatum rounds, including all wait/end/advance timing fields. This is an on-host generator sharing account resources, not 600 real browsers/networks. An isolated browser on the actual Staging origin verified font and IndexedDB reload/offline persistence. 1,002 new synthetic links and 8 group links were deactivated; no data removed. Production source/config hashes were unchanged and all 95 trial health probes succeeded. A later post-trial Production timeout recurred while Staging stayed healthy; one ordinary Passenger restart recovered three successive 200 probes. No Production code/config deployment; its historical intermittent behavior is not permanently resolved. See `docs/UT_DD_RESILIENCE_FA.md` and sanitized `artifacts/ut-dd-resilience-20261103/`.

An initial SSH failure was transient. The private env file had a stale admin value; it was backed up and aligned with the already-active credential without rotating it or printing/transferring it. Keep env/key/backup files outside Git/web. Do not read raw private test logs into tool output; use explicitly whitelisted aggregate JSON counters. Full offline Ultimatum and questionnaire redesign remain outside this change.

Staging runs PR #27 application source `68d22169d8d599d4fe6bc2034873c6a58ab7f593`: LiteSpeed loopback proxy to Uvicorn, two workers on port 8766, source `/home/myexperi/lifetrade-staging/repo`. Each worker has an opt-in limit of eight simultaneous sync handlers and a four-connection PostgreSQL pool. Production retains the Passenger paths below. Read `docs/CAPACITY_FIX_2026-11-03_FA.md` and `ops/README_CAPACITY_FA.md` first. Staging restart uses Supervisor; touching Passenger restart.txt does not restart Uvicorn. Runtime releases must include `request_metrics.py`; the launcher pins both runtime file hashes. Stop the service before replacing dependencies; wait for bounded readiness retries after start and verify public HTML/API.

Earlier tests on source `7af1838` completed 600 API workflows with exact database counts and zero HTTP failures in on-host HTTPS and two-origin tests. External latency thresholds still failed; the single-IP external 600 run was aborted. This is not 600-browser/600-IP or unconditional production readiness. User confirmed real participants use individual internet connections. Synthetic fixtures contain private invitation tokens: never print/commit them or transfer them outside the specifically authorized scope. All current-trial links are deactivated after tests and data retained. No production code/configuration deployment is included.

Remote Desktop Commander plus the user's authorized SSH identity was available. The legacy statement below about unavailable SSH is a historical capability snapshot: re-check current tools and authorization, never request or expose credentials.

## Read first

Before taking action, read in order:
1. `AGENTS.md` (this file).
2. `README_FA.md`.
3. `docs/AI_HANDOFF_FA.md` (the canonical operational handoff; especially the Passenger paths).
4. `EXPERIMENT_ARCHITECTURE.md` and `DATABASE_ARCHITECTURE.md`.
5. `CPANEL_RENAME.md` only for an intentional public-path migration.

Re-check live GitHub state and the host before relying on any SHA, CI result, or dated snapshot.

## Required workflow

- Inspect current branch, `git status`, latest `main`, source files, tests, and current CI before edits.
- Documentation first: update `README_FA.md` and the relevant architecture/operations document before implementation. Include docs, code, and tests in the same logical commit/PR.
- Preserve API contracts, workflow sequencing, backward-compatible entry points, existing participant data, and approved Persian participant copy unless the user requests a change.
- Add/update regression tests. Check CI for the exact commit SHA; do not rely on a successful run for another SHA.
- Do not merge/deploy a moving branch. After merge, re-check main SHA and CI.
- GitHub merge, host deployment, and live verification are three different states. Never claim deployment until the real public page and API have been verified.

## Critical cPanel invariant — do not violate

Observed cPanel values:
- Passenger Application Root: `/home/myexperi/lifetrade`
- Bootstrap: `/home/myexperi/lifetrade/passenger_wsgi.py`
- Bootstrap sets `APP_ROOT=/home/myexperi/lifetrade/repo`, adds it to `sys.path`, then imports `from main import app`.
- Actual active Python source root: **`/home/myexperi/lifetrade/repo`**
- Active participant HTML: **`/home/myexperi/lifetrade/repo/app/index.html`**
- Active assets: `/home/myexperi/lifetrade/repo/app/`
- Restart trigger: `/home/myexperi/lifetrade/tmp/restart.txt`
- Public URL: `https://my-experiment.ir/lifetrade/`

The Application Root is not the Python import root. Publishing to `/home/myexperi/lifetrade/app/` or putting Python files directly in `/home/myexperi/lifetrade/` updates inactive duplicates and does not update the current Passenger app. Deploy runtime files into `.../lifetrade/repo/`. Do not casually overwrite `passenger_wsgi.py`, `.htaccess`, virtualenv, host environment, or local files.

See `docs/AI_HANDOFF_FA.md` before writing any deploy command.

## Data/security invariants

- Production connects through `DATABASE_URL`; the known DB name `myexperi_lifetrade_study` is not a full connection string.
- Never request, print, commit, or document secret values: `DATABASE_URL`, `ADMIN_TOKEN`, invitation tokens, SSH private key, or Fernet key. SSH key path only: `/home/myexperi/.ssh/lifetrade_deploy`.
- Fernet key `.barsan_invitation_tokens.key` must remain outside Git and web root; loss can make encrypted invitation URLs unrecoverable.
- Never run production reset, `DROP`, broad `DELETE`, destructive migration, or cleanup without explicit authorization, scoped preview, and verified independent database backup.
- `init_db()` runs during import/startup and uses additive table/column creation in the current implementation. Rolling back files does not roll back database DDL.
- Never use `git reset --hard`, blanket `git clean`, `rsync --delete`, or copy a local SQLite DB over PostgreSQL.

## Product constraints

- Modules: questionnaire, DD/Life-Trade, Ultimatum. Server owns identity, assignment, consent, workflow order, active attempts, scoring, and persistence.
- Participant interface is Persian/RTL. Do not expose experiment internals, Frame/Control, block labels, or admin information on participant screens unless requested.
- Welcome heading currently approved: «به برنامهٔ پژوهشی بارسان خوش آمدید». Its ordered stages must be fetched from `/api/experiment/workflow?invite=...` and mapped to participant-facing labels only: `questionnaire → پرسشنامه`, `dd_lifetrade → بازی انتخاب`, and all Ultimatum variants → `بازی باشگاه رانندگان بارسان`. Never expose module codes, Frame/Control names, or descriptions. For the legacy default flow, include the Ultimatum stage after questionnaire and DD/Life-Trade because the legacy participant journey starts it automatically after DD.
- Ultimatum opponent parameters and the apparent-coworker manipulation are provisional; never claim ethics approval. Require investigator/ethics confirmation before changing protocol or representing it as production-approved.

## Runtime / CI

- cPanel virtualenv observed: `/home/myexperi/virtualenv/lifetrade/3.12/bin/python`; CI uses Python 3.12.
- CI checks inline JS in `app/index.html` and `app/admin-links.html` using Node, runs `pytest -q`, then `tests/postgres_smoke.py` against PostgreSQL 16.
- `Procfile` and `render.yaml` describe a separate Uvicorn/Render deployment; they do not describe the observed cPanel Passenger bootstrap.
- The assistant has GitHub integration but cannot SSH into the user's cPanel. Provide host commands and request sanitized output; never imply direct host access.
- When a deployment fails, identify the exact failing stage and inspect the actual runtime path/logs. Never repeat a failed script unchanged.
