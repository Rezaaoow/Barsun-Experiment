# Named participant registration — verified two-Production release

Date: 2026-11-05. Requested implementation and functional verification are complete on both independent Production deployments:

| Deployment | Public address | Frozen application commit | Verified runtime files |
| --- | --- | --- | --- |
| New Ubuntu server | https://experiment.barsun4818.ir/dce/ | `742b1f24327e87335e06dc349168572374de0cd8` | 223 |
| Existing cPanel host | https://my-experiment.ir/lifetrade/ | `742b1f24327e87335e06dc349168572374de0cd8` | 223 |

[PR #41](https://github.com/Rezaaoow/barsan-experiment/pull/41) implemented the application changes. Source CI run `42048112146` and merged-main CI run `42049038786` succeeded, including PostgreSQL and browser regressions. Operator-only follow-up `0ca942cc31b120270f57b9f880526c524b15a227` waits for LiteSpeed public route readiness; application files did not change. This results document and handoff updates are also documentation only. The CI-approved deployed application remains `742b1f2`; do not restart it just to publish documentation.

## Changes applied to EACH Production

- One shared registration UI for individual and group entry: mandatory first name, last name and participant code. Added `participants.first_name` and `participants.last_name` without deleting or fabricating historical data. Names are returned by protected identity/admin reads and appended to existing and normalized DD/Ultimatum CSV exports.
- Removed group manual/random choice cards and random-code allocation. A supplied code can claim a shared group link without a pre-created individual invitation. Existing individual invitations, code ownership, fixed condition/workflow snapshots and idempotent claim receipts remain enforced; conflicting existing assignments are not silently reassigned.
- New registration requires names; historical accepted-consent retries can omit them without erasing a profile or breaking an existing task outbox. Timing/answer identities and durable queues are unchanged.
- Removed participant-facing `کد یکتا` wording.
- Inactive, deleted or unknown supplied invitation tokens are checked before returning experiment HTML. The inactive page body contains exactly one paragraph and one button, with no application bundle, registration, branding or navigation:
  - `این لینک غیر فعال است.`
  - `بستن صفحه`
- Added the requested Ultimatum introduction after the initial start button: `در حال تعیین بازیکن مقابل از بین همکاران`, randomized 3–6 seconds, then `بازیکن مقابل تعیین شد` and another `شروع کنیم` button. Existing practice and main-game logic remain intact. This is a timed presentation step, not a new live human matchmaking service; the existing opponent models/manipulation remain unchanged.
- Applied the exact requested participant copy:
  - `در هر دور، باید بین دریافت مبلغ کمتر همین الان و یا مبلغ بیشتر پس از مدت مشخص، یکی را انتخاب کنید.`
  - `در این بازی مبالغ فرضی‌اند و پرداختی به شما صورت نمی‌گیرد`
  - `هر وقت آماده بودید، بازی دوم را شروع کنید.`
  - AI waiting: `منتظر دریافت پاسخ هوش مصنوعی باشید`
  - Coworker waiting: `منتظر دریافت پاسخ همکارتان باشید`

## Independent functional verification

- 136 local functional/browser regressions passed (Chrome/WebKit), plus syntax checks. The extended timer suite was left to successful CI. No load, stress or capacity testing was performed.
- On each live Production, fresh isolated `barsan-test-*` studies checked mandatory names/code, rejection of random allocation, group entry without individual preallocation, identical individual/group registration HTML, stable claim retries, consent retries, preserved names/assignment/condition and correct task-attempt handling.
- Four actual mobile browser flows passed: shared Frame link and individual Control link on EACH Production. Each covered registration, exact DD copy, a queued DD answer during a brief offline interval, reload/re-entry, five DD choices, exact second-game transition, opponent introduction, AI/coworker waiting messages, both practice rounds and retained identity. Zero browser page errors. Observed introduction durations: new 4.83/5.33 seconds; old 5.32/4.32 seconds.
- Reload behavior follows the existing research rule: an incomplete task starts a fresh attempt; the previous attempt becomes `superseded`, and its queued answer remains associated with its original run. Workflow position and participant identity are preserved. This release does not silently change that rule into answer carryover.
- On EACH Production, all four DD/Ultimatum legacy/normalized CSV endpoints contained the added profile columns and correct synthetic names. Current DD export contained 10 fresh-attempt choices; normalized DD contained 12 rows including the two preserved prior-attempt offline answers.
- After testing, all five newly created synthetic group links and five associated individual assignments were deactivated (three groups on new server, including one validation-helper attempt; two on old host). All synthetic data was retained, with no reset or deletion.
- Eight inactive URLs were checked in actual browser tabs: exactly the specified paragraph/button, blocked inactive API entry, and successful closure of script-opened tabs.
- Final public home/admin pages returned 200 and PostgreSQL health succeeded independently on both origins. Participant HTML was identical after normalizing the two URL prefixes. TLS chain/hostname validation passed on both domains.
- Verified health monitoring throughout release: new 533 successful probes, old 537 successful probes; zero failed probes. An earlier pre-deployment monitor using an incomplete local Python CA store was excluded; the monitored release used verified CA trust, never disabled certificate verification.

## Availability, protection and recovery

Both deployments used independently prepared parallel workers, readiness checks, proxy handover and drain. Temporary workers were removed afterward. On cPanel, an initial immediate public-route check still reached old code; the attempt restored the original proxy and left canonical workers running. The revised bounded-readiness procedure completed successfully; no failed monitored health requests occurred.

Old Production's original environment credentials, Supervisor configuration and final proxy bytes were verified unchanged. Its canonical launcher, immutable application release/mirror and release metadata were updated as authorized. Database changes were additive profile columns and isolated synthetic records. A consistent private pre-release dump was verified. Read-only comparison against its COPY data confirmed preservation of all 468 preexisting rows across 23 tables, excluding the explicitly authorized participant profile fields. No existing research records were deleted or overwritten.

New Production's Nginx configuration was restored byte-for-byte, original application service/dependencies/credentials were preserved, temporary unit removed and normal release/backup timers restored. The post-release encrypted backup succeeded (`fc998220f6adf17f4079cbb0370c172455b2e1109e5c6943d0b3329b9b6aa1c3`). Its existing 4 GiB backup volume and free-space guard remain in place. Backups remain on the same server/disk; independent off-server disaster recovery and external alert delivery are still pending.

Protected operational records are under `/var/lib/barsan-operations/registration-742b1f24327e` on Ubuntu and `/home/myexperi/lifetrade/deployments/registration-742b1f24327e` on cPanel. Private synthetic fixtures/screenshots and aggregate reports on the operator Mac are under `/Users/rezaaoow/barsan-loadtest/registration-production-20261105/`; do not copy fixtures, raw dumps or credentials into Git.

## Staging retired

The old separate Staging Supervisor was stopped/shut down; its autostart/autorestart options and only its own reboot cron were disabled/removed. Its own web roots now return HTTP 410, verified publicly. Existing Staging files/database were preserved as an archive; previous configs/cron are saved privately under `/home/myexperi/lifetrade-staging/retirement-20261105`. No Staging application release, functional test or new Staging environment was created. Future work/tests target the two independent Productions with isolated synthetic data.

## Remaining limitations and security follow-up

Browsers control `window.close()`. The button closes script-opened tabs, as verified; browsers may refuse to close tabs opened directly from SMS or browser UI. The page still contains only the requested message/button, with no extra fallback content. See [MDN Window.close](https://developer.mozilla.org/en-US/docs/Web/API/Window/close).

During inspection, an overly broad output filter included the old database credential in private tool output. It was not copied into Git, this report or public files. The credential was preserved during this availability-sensitive release; a coordinated credential rotation remains a security follow-up. Never repeat its value in any output.

No capacity or 600-participant readiness claim is made by these health checks. The requested performance/reliability phase has not begun.
