# Ultimatum copy, icons and offer spread — 2026-11-05

Scope: NEW Production only. Old hosting and Staging are not accessed or changed.

- Exact selection copy: `در حال انتخاب بازیکن مقابل شما از بین همکاران در سالن...`; then `بازیکن مقابل شما انتخاب شد`.
- Exact welcome paragraph: `شما برای کسب امتیاز باشگاه رانندگان با هوش‌مصنوعی بارسان یا یکی از همکاران حاضر در سالن بازی می‌کنید؛ در هر دور ۱۰ امتیاز بین شما و بازیکن مقابل شما تقسیم می‌شود.`
- Restore existing robot/person icons by replacing the CSS rule that hides them. Keep the appropriate text beside the icon; use a responsive 42–52px neutral icon container without introducing new artwork/dependencies.
- Proposers wait for `پاسخ`; responders wait for `پیشنهاد`. AI and coworker names remain condition-specific.
- Set the existing discrete truncated normal offer model's configured spread from 4.0 to 3.0, retaining mean 5, support 0..10, rounding/model structure and acceptance algorithm. The configured SD is the underlying normal parameter; bounded discrete realized variance need not equal 9. Existing stored outcomes/parameters are not rewritten. Future generated offers use the new value.

The user explicitly forbids test runs for this release. Do not execute syntax checks, unit/browser/synthetic/health-flow/load/capacity tests. Deployment readiness is limited to observing actual worker startup, existing startup guards and file/service state. Preserve verified backups, immutable release, parallel routing/drain and rollback. A frozen one-time operator approval replaces full CI for this requested release only; normal automatic CI workflows/privileged guards remain unchanged. Do not claim testing was performed.

Already-open tabs retain their loaded JavaScript until reloaded; do not force participants to refresh. Existing refresh/new-attempt rules remain in place. Stored or prefetched offers retain their original model metadata.
