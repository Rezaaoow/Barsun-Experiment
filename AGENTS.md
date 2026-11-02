# AI Agent Instructions — BarSan Experiment

## Current staging runtime exception — verified 2026-11-03

Staging is now a native ASGI trial: LiteSpeed loopback reverse proxy to Uvicorn, two workers, port 8766, source `/home/myexperi/lifetrade-staging/repo`, fixed PR #25 SHA `e2b725cd0e1924d2dfcbb16f3cbb19d59496bd64`. Production retains the Passenger paths below. Read `docs/NATIVE_ASGI_STAGING_2026-11-03_FA.md` before either environment is changed. Staging restart uses its private Supervisor configuration; touching Passenger restart.txt does not restart Uvicorn. The launcher pins the backend and request_metrics.py hashes: a later code release requires reviewing and updating that guard as well as verifying exact-SHA CI.

The public 100-VU native test passed; 600 VUs did not. Production health was intermittent at the end of the trial, despite unchanged source/configuration. Do not claim production readiness or rerun a large test without fresh health checks.

Remote Desktop Commander plus the user's authorized SSH identity was available during this session. The legacy statement below about unavailable SSH is a historical capability snapshot: re-check current tools and authorization; never request or expose credentials.


This repository backs a live behavioral-research application. Participant records, PostgreSQL, invitation URLs, admin credentials, and the cPanel host are production-sensitive.

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
