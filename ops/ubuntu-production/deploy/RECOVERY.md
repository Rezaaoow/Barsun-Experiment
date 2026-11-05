# Operator recovery runbook

Production: https://experiment.barsun4818.ir/dce/. Routine SSH: barsan-ops@37.152.183.1 with the operator's existing Mac key. Privileged commands use sudo. The old cPanel host is never a recovery target for this deployment.

## Inspect safely

Run `sudo python3 /usr/local/lib/barsan/backup.py status` and inspect `/var/lib/barsan-operations/health.json` plus `/var/lib/barsan-operations/deployment.json`. The status files contain aggregate operational metadata, not tokens/research payloads. Check `systemctl status` for barsan-production, barsan-backup.timer, barsan-restore-check.timer, barsan-health.timer, barsan-release.timer and the backup mount. Use `journalctl --namespace=barsan` for bounded service logs. Do not paste raw private configuration or arbitrary SQL errors into chat.

The new Admin Token and separate backup recovery password were saved to the operator's private Mac file `/Users/rezaaoow/.config/barsan-production/access-20261105.json`, directory 0700/file 0600. It is outside the project/Git. Open it privately; never print its contents into logs/chat. This is a recovery-password copy, not an off-server data backup.

## Verify a recoverable backup

Run `sudo python3 /usr/local/lib/barsan/backup.py restore-test`. This checks encrypted repository data, recovers/hash-checks dump/configuration files in bounded RAM, restores into a uniquely marked temporary database and verifies exact snapshot row counts. It checks free root space for the 8 GiB reserve plus twice the DB size before creating the scratch database. It does not overwrite Production. Confirm restore_verified=true; skipped due to operations_lock_busy is not a completed drill. Daily timer is 02:17 UTC / 05:47 Asia/Tehran. Local health flags failures/overdue drills; external notification is not configured.

## Recover application code

For a failed application release, the installed release agent returns to previous code automatically if health fails, then verifies previous-code health. For deliberate operator rollback, first inspect whether real runs are active, stop the polling timer to avoid an immediate approved-ref promotion, then use `sudo python3 /usr/local/lib/barsan/release.py rollback`. The agent requires a successful backup and normally defers when active real research exists. Do not use --allow-active casually. Confirm public health and the selected commit. To return to the approved release, use the poll action and re-enable/start barsan-release.timer. Code rollback preserves the current database; additive schema changes are not undone.

## Recover database/configuration

Never automatically restore an older database over live responses. Select the intended encrypted snapshot, understand its age/loss window and preserve the current database before a deliberate maintenance operation. Restore a candidate into an isolated database first and verify it. Switching live databases requires stopping application writes, preserving the current database for audit, applying the correct roles/grants and explicitly choosing the accepted recovery point. A blanket DROP/reset of barsan_production is not a recovery shortcut.

Recover the matching invitation key and app configuration as well as database data. Preserve the NEW server's local-only database target and canonical URL; never restore an old cPanel DSN. The configuration archive includes private material: unpack only reviewed whitelisted paths into a protected candidate directory first, preserve current SSH/recovery access, and restore ownership/permissions for actual new-host identities. Do not blindly unpack it over /etc or replace host SSH keys. Reconstruct the pinned runtime from hash-verified wheels and the matching release, then verify startup guard, public HTTPS/database health and a synthetic acceptance flow.

Local Restic repository: `/var/backups/barsan/repository/restic`, cache within the same 4 GiB capped mount. Password file: `/etc/barsan-backup/password` (root 0600). An unmounted backup volume must never fall back to writing into the root directory. Existing repository/image/credentials must not be reinitialized or overwritten during recovery.

## Complete server/disk loss

Current local data and backup repository share one disk. Losing it can lose both; the separately retained password cannot reconstruct missing backup data. Provision/recover a server and restore an independently retained data copy only when one exists. No independent copy or provider snapshot is currently verified. Future S3/SFTP replication can use the same Restic snapshots/capture pipeline; verify remote restore and retain its password independently before claiming disaster protection.
