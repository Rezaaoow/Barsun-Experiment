# Final counterbalancing and reliability acceptance

## Authorization and preserved baseline

The current user explicitly requests implementation, comprehensive regression/recovery and a 600-participant test against NEW Production only. This supersedes earlier no-test instructions. Do not access old hosting or Staging. Existing finalized group URLs, tokens, active flags and saved workflow snapshots must remain unchanged. Use only newly generated, labeled synthetic `barsan-test-*` studies and participants. Capture/verify preexisting group-link integrity without exposing tokens or PII.

Frozen baseline: `1cd8314b803f5510263435631ae69a8251091b95`; GitHub rollback branch `codex/rollback-before-final-20261105`. Verified 223-file server copy plus protected config/release metadata retained outside normal cleanup at `/opt/barsan/rollback-baselines/pre-final-20261105-1cd8314b803f`. Pre-change encrypted backup: `4bf068429e33b023fee76fa0a428d5a67405dd265bcb4d8bbb8f999d4903cf8f`. Recovery is code-only unless an explicitly separate DB restore is needed; never discard newer responses by restoring a stale database over them.

## Planned implementation

1. Add dedicated randomization streams and immutable participant/study/module-occurrence allocations. Existing invitations/workflow snapshots are not edited. All 24 permutations of four main Ultimatum blocks appear once per 24-ticket cycle. Randomized Williams quartets within each cycle balance positions and immediate predecessors in completed four-ticket sub-batches; all 24 orders remain represented. Separate Frame/Control and study streams; do not change group membership. Short atomic allocation in the existing start request, no added intertrial network call. Copy stable plan/algorithm version/ticket metadata into each new attempt; legacy attempts retain their existing plan, and stored responses are never rewritten.
2. Prepare stable balanced DD left/right plans once; retain five-trial adaptive logic and full-module behavior. Keep intentional fixation/intertrial/opponent waits. Use existing IndexedDB/timing/outbox infrastructure.
3. Inspect stale DB connection, process hang, cached-page and browser-lock causes of historical refresh symptoms. Implement only justified recovery improvements, bound waits and preserve data/idempotency. Check connection health and actual query/worker metrics before changing resource limits. No blind periodic worker restarts during research.
4. Full local/CI SQLite/PostgreSQL/concurrency/browser regressions, then independent Production synthetic smoke/fault/re-entry/long-idle checks. Freeze, backup and parallel-deploy the accepted source. No participant-data reset or unscoped link cleanup.
5. Ramp toward 600 active sessions, run paced realistic participant journeys plus synchronized entry pressure and retry/recovery cases; measure initial allocation separately from task RTT and server SQL/pool time. Protect resource reserves and abort on sustained failures. Confirm exact DD/Ultimatum counts, scores, timing receipts, stable allocations and no duplicate responses.

## Network realism and limits

The user confirmed that no third machine is available. Available generators: Mac (external HTTPS) and new server (second origin, sharing host resources). Use independent sessions and varied network/think-time/connection profiles. Injected RTT/outages are simulations, not 600 physical ISP routes; report actual origins, engine/browser coverage and generator contention honestly. Do not fake `X-Forwarded-For` and call it multiple internets. Mac has k6 installed; preserve raw private fixtures/logs outside Git and publish sanitized aggregates only.

## Progress

- Access, live baseline, service/backup health verified; zero active real incomplete runs at initial audit.
- Rollback source branch and independent server runtime/config preservation completed; pre-change backup succeeded.
- Implementation/tests/load acceptance pending. No new changes are deployed yet.

### Concrete refresh/retry defects found during source audit

- Psycopg 3.2.6 supports checking stale pooled connections; add checkout validation, finite connection lifetime, 15-second statement and 30-second idle-transaction bounds. Keep the existing bounded pool/admission timeout and measure added checkout cost.
- A cached IndexedDB handle could remain closed and cause every later transaction to fail until refresh. Invalidate it on close and safely reopen once only when a transaction never started; never delete the database or silently replay an ambiguously committed transaction.
- Ultimatum recovery could swallow a failed resolve and clear its own scheduled retry. A retry firing during the prescribed proposer wait could also skip the pending decision and clear the timer. Propagate recovery failures and reschedule deferred decisions; show a neutral automatic-connection-recovery state instead of requiring refresh. Retain the exact saved decision for retry, including after a lost acknowledgement/timing-write failure; do not invent missing research timestamps.
- Add no-store to admin HTML; participant HTML already has it. No evidence establishes that the old host's historical symptom had one particular cause; these address concrete current failure paths without blind periodic restarts.

- Additional entry-page defect: the homepage caught every HTTP exception as an inactive invitation. Now only explicitly tagged inactive/invalid links get that page; a transient 503 uses a neutral, uncached automatic retry of the exact same URL (2–5 second jitter), without rendering an unverified registration form or editing any link. Chrome/WebKit automatic-retry coverage is included.
