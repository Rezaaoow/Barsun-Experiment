# Participant registration and two-Production release

Current user authorization supersedes historical migration restrictions: apply this narrowly scoped release independently to NEW Production https://experiment.barsun4818.ir/dce/ and OLD Production https://my-experiment.ir/lifetrade/. Preserve old-host availability, existing research rows, credentials, runtime dependencies and unrelated infrastructure. Do not use or deploy Staging. Retire its isolated service/automatic startup and public entry without deleting archived data. No load, stress or capacity tests in this phase.

Both invitation types share one registration form: required first name, last name and code. Group links allocate an assignment for the submitted code directly, using the group's frozen condition/workflow; no individually pre-created invitation is required. Remove manual/random selection and random allocation from the public group entry. Individual token/code ownership, stable participant IDs, existing condition/workflow, consent and replay semantics remain. Names are additive participant information; historical rows remain blank where names were never collected. A previously accepted consent can still replay without new profile fields so already-running old clients/outboxes keep working. New consent/registration requires the complete profile. Do not put names into task-answer queues or operational logs.

Add only first_name and last_name to participants with empty defaults; use bounded additive schema initialization and no destructive backfill. Expose the collected identity fields through existing participant/record exports without removing old columns. Valid names are trimmed, nonempty and bounded; no external identity/membership service or matching algorithm is introduced. Retries preserve the existing participant/assignment rather than creating a new one.

Resolve supplied invitation/group tokens on the server before serving participant HTML. Missing/invalid/deactivated supplied tokens receive a standalone page with only `این لینک غیر فعال است.` and a `بستن صفحه` button. No welcome, registration, experiment JS, branding, navigation or diagnostic text. The close button requests window.close(); browser security can refuse closure of a manually/SMS-opened tab. Do not add fallback text/content or claim JavaScript can override browser restrictions. APIs must continue rejecting inactive links; a deactivation race during entry returns the same inactive state rather than an ordinary registration error.

Exact participant copy:

- DD: `در هر دور، باید بین دریافت مبلغ کمتر همین الان و یا مبلغ بیشتر پس از مدت مشخص، یکی را انتخاب کنید.`
- Payment: `در این بازی مبالغ فرضی‌اند و پرداختی به شما صورت نمی‌گیرد`
- Transition to game two: `هر وقت آماده بودید، بازی دوم را شروع کنید.`
- AI wait: `منتظر دریافت پاسخ هوش مصنوعی باشید`
- Coworker wait: `منتظر دریافت پاسخ همکارتان باشید`
- Initial opponent selection: `در حال تعیین بازیکن مقابل از بین همکاران`
- After a randomized 3000–6000 ms: `بازیکن مقابل تعیین شد`, then `شروع کنیم`.

Select waiting copy from the trusted round's agent/coworker condition. Preserve existing server outcomes, practice rounds, task order, stimulus timing and durable IndexedDB/retry/completion pipelines. The requested introductory screen is a presentation step before practice/main play; it does not implement real multiplayer matchmaking or change the existing coworker-condition simulation. Keep its presentation state/timestamps within the existing run/timing metadata, not a separate persistence/network mechanism. No unapproved UI redesign or participant copy changes. Replace participant-facing `کد یکتا` with `کد` consistently.

Before each release: backup current DB/config and verify safety of the additive migration. Existing real runs are active on old Production; use parallel workers and a verified proxy handover/drain, never stop the only serving listener. Preserve final canonical routing/configuration and record necessary temporary deployment switches honestly. Synthetic functional checks are scoped to new test studies separately on each Production deployment; retain results, deactivate only this test's links, and do not reset real data. Validate mandatory fields, manual code group access without individual preassignment, replay/resumption, both link types, minimal inactive DOM, exact copy and the timed intro. Record the browser-close limitation and historical blank names in the release report.


## Release preparation and checks

The GitHub connector was reconnected and main f318f4899b0906ef88e3c545195385cbce10c1c0 verified. Application changes have passed 136 local functional/browser regressions (Chrome and WebKit; the unrelated extended timer suite was excluded locally), plus JS syntax. No load/capacity test ran. CI for the frozen release remains a separate gate.

Temporary parallel-worker release scripts are operator tools, not new permanent architecture. Ubuntu uses its existing CI-approved ref, encrypted backup, shared operation locks, independent app user and dependencies; a temporary Nginx switch serves the candidate while canonical workers change. cPanel uses a verified private consistent dump, data digests excluding additive profile fields, existing credentials, candidate Uvicorn on loopback 8768, proxy handover and drain; original proxy/env/Supervisor configuration is restored/preserved. Existing research rows are not overwritten or removed. Code rollback never restores an old database over newer responses.

The existing Staging application is retired by stopping its separate Supervisor, disabling its restart options/removing only its own reboot cron, and returning HTTP 410 from its own web roots. Its files, database and previous configurations remain archived; Production resources are checked independently.
