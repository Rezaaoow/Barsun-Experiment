# Operations step and old-host preservation

These files implement only verified operations access on the new Ubuntu Production server 37.152.183.1. Canonical URL: https://experiment.barsun4818.ir/dce/. No application Staging deployment is permitted. All future deployment/load/stress/capacity tests use isolated synthetic data against this NEW Production deployment.

The old cPanel hosting deployment must remain fully operational and untouched throughout migration. No old-host deployment, configuration, restart, disablement, link closure, DNS/redirect change, data mutation, load test, cutover or retirement is authorized. Never run the legacy cPanel release scripts for this migration. Read-only audit/snapshot access must preserve availability; any imported snapshot remains independent, not synchronized back to the old database. This user requirement supersedes earlier cutover/maintenance proposals.

Preserve all existing provider-managed SSH files and root authorized keys. Apply a separate early key-only drop-in only after a fresh external operations SSH/sudo proof, validated syntax, protected backups and an armed timed rollback. Cancel rollback only after fresh operations and root-key recovery connections and advertised-method verification succeed. Keep private keys and credentials off Git/output; accept public keys only as explicit input, with fingerprint verification.

The operations identity has administrative sudo; it is separate from the repository reader/application identities. Do not grant sudo to barsan-deploy or copy the operator's private key onto the server. Root-key recovery remains available; provider-console and reboot recovery have not been verified. Do not infer complete production readiness from SSH or TLS health while /dce/ is still maintenance 503.


## TLS requirements retained


The user's current instructions supersede historical staging policies in the repository root: deploy Production only to 37.152.183.1, canonical https://experiment.barsun4818.ir/dce/. Do not create or migrate an application Staging environment. All deployment/load/stress/capacity testing must target Production using explicitly isolated synthetic data, preserving real research records.

Inspect existing services, credentials, DNS, certificate lineages and configuration before any change. Reuse valid existing certificate/configuration; never rerun the fresh-host bootstrap over a working server. Preserve existing recovery access and configuration backups. No private key, ACME account, password, admin token, real invitation URL or research payload may be printed or committed.

Certificate issuance requires verified DNS, HTTP-01 webroot reachability, inbound 80/443, outbound ACME HTTPS and preserved SSH access. Automatic renewal must be enabled and actually tested with a successful dry-run and observed Nginx reload hook. The test CA used by Certbot is not an application Staging deployment.

TLS preparation does not complete application migration. The current /dce/ maintenance 503 is intentional; never declare application production-ready from a valid certificate or an expected-status 503 check. Final readiness requires the deployed application at /dce/, valid hostname/chain, successful renewal verification, healthy database, recovery procedures and isolated Production acceptance checks.

This directory contains non-secret configuration and bounded bootstrap procedures. Sensitive operation logs, certificate/account keys and network proof remain root-only on the server. App installation, database migration, cutover, operational SSH hardening and complete CI/CD are separate sequenced tasks; do not infer completion from this TLS step.
