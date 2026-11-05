# New Production: final counterbalancing and reliability acceptance

Canonical URL: https://experiment.barsun4818.ir/dce/ . Application code `e859767f57008c7a89d231b14ff003349318a2cb`, 225 verified runtime files. Production only; old hosting and retired Staging were not accessed or changed. This operations/evidence revision retains identical application bytes and records the tested four-worker configuration.

## Implemented

- **Ultimatum:** immutable full-24 permutation allocation, atomic PostgreSQL cursor/plan storage in the existing start transaction, separate study/Frame-Control/module-occurrence streams. Six randomized Williams quartets cover all 24 orders once per cycle; completed four-ticket groups balance positions and immediate predecessors among main blocks. Seeds remain server-side. No extra per-trial allocation call. Existing saved legacy orders and responses remain intact; retries and fresh attempts retain the participant's plan. Allocation balance does not guarantee completion balance if real participants drop out. Method reference: [balanced Latin-square construction](https://statpages.info/latinsq.html).
- **Standalone DD:** save one shuffled five-position plan with two or three left-side immediate options. Preserve the adaptive questions, amounts and actual choice/timing records.
- **Database recovery:** validate pooled connections, retire them after 30 minutes, bound statements to 15 seconds and idle transactions to 30 seconds. Return retryable 503/Retry-After on transient connection/admission failures; never blindly replay writes on the server. Serialize additive schema initialization across workers.
- **Browser persistence:** reopen a closed IndexedDB handle once only when no transaction started. Preserve existing records; never delete the database or replay an ambiguously committed transaction.
- **Ultimatum retry:** fix swallowed recovery errors and a retry timer cleared during the prescribed proposer wait. Retain the exact saved decision, automatically retry transient failures and reschedule deferred recovery. Do not invent missing timestamps.
- **Entry recovery:** only explicitly inactive/invalid links receive the inactive-link page. A temporary database 503 gets a neutral, uncached automatic retry of the same URL after 2–5 seconds, retaining its query and browser storage. Admin HTML is also uncached.
- **Measured capacity tuning:** four Uvicorn workers, eight sync handlers and at most four DB connections per worker (16 total). The installed Uvicorn 0.34.0 SIGTTIN implementation was inspected; two workers were added without stopping the original workers. Persisted and validated the systemd unit, with protected before/after copies. No dependency, kernel, firewall, protocol, offer SD4, scoring, intentional delay or research UI redesign was introduced.

## Final 600-session acceptance

`distributed600c`: **all thresholds passed**, both generators exit 0, 600 completed, zero HTTP failures, zero terminal failures and zero unplanned retries. Duration approximately 220 seconds. Independent sessions and variable application-layer RTT/think-time profiles; 100 external Mac sessions plus 500 sessions on the new server. This is **two physical origins, not 600 ISPs or browsers**. The on-server generator shares application resources.

PostgreSQL timestamps confirm a peak of **600 simultaneously active Ultimatum runs**, not merely 600 allocated virtual users.

| Measurement | External Mac, 100 sessions | On-server, 500 sessions |
|---|---:|---:|
| HTTP requests | 5,728 | 28,636 |
| HTTP p95 | 679 ms | 53 ms |
| HTTP p99 | 1,661 ms | 98 ms |
| HTTP maximum, including cold page/font | 10,076 ms | 323 ms |
| Ultimatum start p95 | 633 ms | 49 ms |
| Allocation section p95, including lock wait | 9.42 ms | 8.21 ms |

Exact verification: **3,000 DD answers, 13,200 completed Ultimatum rounds (12,000 main plus 1,200 practice), 13,200 complete round timing records and 1,800 closed visible-time clocks**. No missing/duplicate round rows. All 600 allocation tickets are unique within their streams; 12 intentionally restarted attempts retain their exact plans. 150 deliberate lost-ack replays are separate from the zero unplanned retry count.

Each Frame/Control stratum has 300 participants. All 24 orders appear 12 or 13 times; each block occurs **75 times at each position**, and each of the 12 directed main-block predecessor pairs occurs 75 times. Zero semantic mismatches between allocation records and every run's saved plan.

Live authenticated CSV verification checked all 600 runs/13,200 rows: `final_score == cumulative_score`, practice excluded, separate run totals correct. No exported participant rows or credentials were transferred off-server for this check.

Maximum sampled app memory was **297 MiB** against a 1 GiB limit; maximum sampled host CPU busy was approximately 72% including the generator. Root free space stayed above 37.6 GiB, preserving the 8 GiB reserve. All 39 health probes succeeded.

## Stress results and disclosed failures

These results are retained rather than relabeled as clean passes:

- Paced 24-session run: completed, no HTTP failures/retries.
- External 100-session rapid run: all 100 completed with exact data, but 84 transport timeouts and 38 recovered API retries failed raw HTTP/latency thresholds. A single external conduit is not 100 separate connections to independent ISPs.
- On-server rapid 300-session run: no HTTP failures/retries; p95 latency exceeded 1 second. Metrics isolated handler admission queueing (~2.2s p95 before DB access) versus SQL (~36ms p95). Adding workers increased throughput from ~4.48 to ~6.55 completed journeys/second; start p95 fell ~2.84s to ~1.48s. The repeat rapid run still exceeded the one-second overall target.
- First 300+300 attempt was aborted by a coordinator SSH-probe timeout. The coordinator was fixed to tolerate probe timeout and reuse a protected multiplexed SSH connection. An orphan belonging only to that test was stopped. Partial synthetic data remains retained.
- `distributed600b`: all 600 completed, zero HTTP failures/retries and exact data. A harness comparison incorrectly treated different JSON object-key ordering as different plans, failing 11 assertions. PostgreSQL semantic comparison found zero mismatches across 612 attempts. The harness was corrected and the entire clean `distributed600c` run repeated.
- **Synchronized 600-start burst:** all allocated successfully, all identity/plan checks passed, zero HTTP failures/retries, exact 24-order/position/carryover balance. Start p95 **2.14s**, maximum **2.34s**. Overall p95/p99 exceeded the artificial 1s/3s stress thresholds. This is bounded initial queueing, not a zero-latency result, and not a second full journey run.
- One external handover probe had a transport timeout; 119/120 succeeded. The next probe recovered. Do not claim universal zero downtime from this observation.

See [sanitized machine-readable evidence](../artifacts/production-final-20261106/acceptance.json). Private fixtures, tokens, raw logs and screenshots stay outside Git.

## Recovery and backups

GitHub Actions application run [42106031999](https://github.com/Rezaaoow/barsan-experiment/actions/runs/42106031999): **166 tests passed**, including Chrome/WebKit and PostgreSQL/concurrent-allocation integration. Production browser journeys independently completed all modules/22 rounds with offline DD storage, lost-ack recovery, 600 seconds idle, zero pending entries, zero page exceptions and no manual retry/refresh. Chrome injected four failed resolve attempts, WebKit one.

With zero real active runs, two idle pooled connections were terminated: all subsequent health calls succeeded without app restart. A single worker was killed: the native supervisor replaced it, retained four workers and the same parent process; all 23 observed health requests succeeded. These are controlled tests, not a guarantee against every possible fault.

Fresh encrypted post-test backup `b33282183c9ad1d39de917e45ad11a2ade6747831214cfde8255d0c44ac9602d` captured approximately 48.4 MB. Isolated restoration verified all **23 table counts** and every captured file hash; no Production DB was restored or overwritten. The encrypted repository remains hard-capped at 4 GiB with the 8 GiB root reserve. Backups remain on the **same physical server**; disk/server loss is not covered until independent storage is supplied. Independent external alert delivery is also still unavailable.

Normal automation remains GitHub main → CI success → production-approved ref → two-minute release polling, with frozen source, fresh backup, deferral during real active runs and code-only rollback. Privileged operations code and dependencies are not self-updated. Runtime-identical operations/evidence revisions may have their manifest advanced only after CI approval and exact runtime/unit hash comparison, without restarting workers.

## Existing invitation protection

All original 11 group tokens, workflow snapshots, labels and other non-status fields match the sealed original records byte-for-byte. All 12 original individual assignments and the signing key are unchanged. Two existing group active flags were changed through four admin-panel requests **by the user**, who explicitly confirmed this; their current settings were preserved, not restored or overridden.

Only **22 newly created synthetic group links** from this operation were deactivated; their data was retained. Cleanup explicitly rejects every preexisting group ID, including preexisting test/review links. No original link was replaced, reissued, deleted, rotated or invalidated by this rollout. Real study allocation streams are independent of every synthetic study stream.

## Code rollback

Preserved application baseline: `1cd8314b803f5510263435631ae69a8251091b95` on GitHub branch `codex/rollback-before-final-20261105`. Verified 223-file server copy, metadata and protected config are retained outside cleanup at `/opt/barsan/rollback-baselines/pre-final-20261105-1cd8314b803f`. The initial encrypted snapshot is `4bf068429e33b023fee76fa0a428d5a67405dd265bcb4d8bbb8f999d4903cf8f`.

Operator procedure if reverting this rollout:

1. Stop `barsan-release.timer` so the approved newer ref is not redeployed automatically.
2. Verify the recorded previous release is the preserved baseline. For the previous worker configuration, restore the protected `/var/lib/barsan-operations/final-20261105/worker-tuning/service.before` unit to `/etc/systemd/system/barsan-production.service` and run `systemctl daemon-reload`.
3. Run `sudo python3 /usr/local/lib/barsan/release.py rollback`; its real-active-run guard, file validation and health checks remain enabled. Inspect the protected deployment status if it defers.
4. Verify HTTPS, database health and invitation integrity. Keep polling paused until a corrected CI-approved release is selected.

**Never restore a pre-test database over newer research answers or the user's current invitation status settings to roll back code.** Added randomization tables are backward compatible and may remain present.
