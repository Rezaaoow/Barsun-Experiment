# Current local backup authorization

User approved this local-only encrypted backup architecture while independent storage is unavailable, specifically requiring bounded disk consumption. User chose a clean new Production database. No old-host access, import or mutation is needed. Current user authorization supersedes earlier requirements to wait for off-server storage before deployment. State its absence honestly.

Never overwrite a credential, repository image, existing configuration or populated database. Back up and inspect existing resources first. Preserve SSH/TLS/network setup. Do not add an application Staging environment. Recovery drill databases are temporary isolated verification resources, not another deployed environment; remove only the database created/marked by the current drill, never real databases or research records. Tests use the new Production deployment and isolated synthetic data.

Root-owned backup/deployment code must be installed from verified frozen GitHub contents; never execute mutable repository code as root without checking the expected commit/tree/content. All cleanup is bounded to owned backup temp paths, this backup repository, or explicitly marked recovery resources. Keep secrets out of output/Git. Local-only backups and local status are not independent recovery/monitoring.
