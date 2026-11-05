# Ubuntu Production runtime

New server only: 37.152.183.1. Canonical public URL: https://experiment.barsun4818.ir/dce/. No application Staging service or database. Old cPanel hosting remains independent and untouched; never invoke its deployment scripts for this migration.

## Boundaries and identities

- `barsan-ops`: administrative SSH/sudo using the operator's existing key.
- `barsan-deploy`: read-only GitHub key and private repository checkout, no sudo.
- `barsan-app`: password-locked, noninteractive system identity, no sudo or SSH key. It cannot read the private repository identity's key.
- PostgreSQL 16: one `barsan_production` database, owned by `barsan-app`. No IP listener. The local socket authenticates the matching OS identity using `peer`; the repository identity cannot impersonate it.
- `/etc/barsan/database.env`: root:barsan-app, 0640. Contains only a local Unix-socket DSN, not the old hosting DSN. Never copy old environment files wholesale.
- `/var/lib/barsan`: app-owned mutable state, 0700. `/opt/barsan`: root-owned code/runtime parent. Future immutable application releases must precreate `.bulk-export.lock` as a symlink to `/var/lib/barsan/bulk-export.lock`, preserving the shared export limit without making application code writable.

These resources alone do not deploy the application or import research data. No application administrator token, invitation key or systemd application service is created by this runtime step. The database must remain empty after the rollback-only synthetic transaction check.

## Dependencies and reproducible recovery

`runtime-versions.json` records the 25 runtime dependency versions from successful existing CI run 41918582983, job 126884734806. This is provenance, not a fresh full CI or security audit. The application requirements remain unchanged. Installation must independently verify compatible wheels, official release metadata, complete SHA-256 hashes, the current source requirements including extras, and `pip check`.

The server's official PyPI CDN connection stalled during large full responses. `download_official_wheels.py` uses verified HTTPS to official `pypi.org` release metadata and `files.pythonhosted.org` byte ranges. It rejects redirects to another host, incompatible/yanked releases, wrong response ranges, wrong length and complete-file hash mismatch. Four concurrent package downloads, bounded request sizes/timeouts and retries avoid an unbounded queue. No mirror, `--trusted-host`, disabled TLS check or changed dependency version is used.

Run the downloader with the target CPython 3.12 Linux amd64 venv, passing the versions manifest, a dedicated wheel-cache directory, lock-file path and manifest path. Existing completed wheels must match official hashes; unexpected/partial files require inspection. Do not overwrite a working runtime in place: create a fresh venv and install the lock offline with `--no-index --find-links CACHE --require-hashes -r LOCK`, then check consistency and source requirements. Preserve the previous venv until the replacement passes application acceptance. This cache allows runtime reconstruction without PyPI availability. Protect the cache and configuration from app/deployment-user writes.

Hash verification proves downloaded bytes match official metadata. It does not prove absence of vulnerable dependencies; package security review remains a deployment readiness task.

## OS maintenance and recovery evidence

The user confirmed independent provider console/rescue access before reboot. The existing 24.04 update plan was inspected; 38 packages updated with no removals and no distribution upgrade. Protected package-version records, configuration archive, globals and an empty-database dump were saved under `/var/lib/barsan-bootstrap/2026-11-05-system-update`. Checked configuration fingerprints were preserved. The previous kernel remains available for console recovery.

The new server then booted from kernel 6.8.0-138 to 6.8.0-147. Fresh operations/root key access, key-only SSH advertisement, sudo, Nginx, firewall, socket-only PostgreSQL, renewal timer and externally trusted HTTPS maintenance response were verified. No reboot remained pending. This proves restart recovery of current infrastructure, not a complete system restore or application/database recovery drill.

Backups in `/var/lib/barsan-bootstrap` are root-only preparation evidence on the same machine. They do not substitute for encrypted independent off-server backups. Full backup/restore, monitoring, automated release/rollback, application/data deployment and isolated synthetic Production acceptance remain separate prerequisites. Keep `/dce/` in intentional maintenance until those gates pass; expected HTTP 503 is not application readiness.
