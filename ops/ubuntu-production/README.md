# Ubuntu Production TLS preparation

Production only: `https://experiment.barsun4818.ir/dce/` on `37.152.183.1`.

This is the TLS infrastructure step, not an application/data migration. The current HTTPS virtual host intentionally returns 503 under `/dce/` until the FastAPI application is deployed. Do not declare the experiment production-ready from a valid certificate alone. The public verifier defaults to expecting an HTTP 200 app plus healthy PostgreSQL; `--expected-status 503` is exclusively the documented infrastructure-phase check.

## Verified prerequisites and changes

Before changes, inspect DNS A/AAAA/CAA (including inherited CAA), authoritative nameservers, active services, listening ports, existing Nginx/Certbot configuration and certificates, SSH access and firewall rules. Reuse valid existing certificates/configuration. The bootstrap here is deliberately for the audited fresh host only: it aborts if Nginx, Let's Encrypt directories or the audit job already exist. It must not be blindly rerun over an existing installation.

For this host, A was confirmed as 37.152.183.1 by the operator resolver, Cloudflare and both authoritative Arvan nameservers. No AAAA or CAA restriction was observed. Google DNS queries from the operator network failed and were not counted as successful checks. Server outbound HTTPS to production and test ACME directory endpoints succeeded. No DNS API credentials or DNS edits were needed; validation uses HTTP-01.

`bootstrap_tls_http.py` installs signed Ubuntu Nginx/Certbot packages, backs up firewall files and new package defaults, enables a domain-specific challenge webroot, retains SSH port 22 and allows web ports 80/443 in UFW. It briefly starts a bounded temporary TCP listener on 443 solely to test inbound reachability; no temporary certificate or app staging environment is created. Verify the public HTTP challenge marker, TCP 443 and a fresh SSH login before recording root-only `network-verified.json` under `/var/lib/barsan-bootstrap/2026-11-05-tls/`.

`issue_tls.py` requires that recent proof and rechecks HTTP and outbound ACME before requesting a certificate. It creates the production certificate only once, enables TLS 1.2/1.3, redirects HTTP to the fixed HTTPS host while leaving the ACME webroot accessible, redirects `/` and `/dce` to `/dce/` preserving query parameters, and enables Certbot's packaged systemd timer. The temporary 443 probe stops before the TLS listener starts.

No account email was supplied; the optional email remains unset. No private keys, ACME accounts, real invitation URLs or personal credentials belong in Git. Certificate/key paths in the Nginx configuration are references only. The private key remains root-owned mode 0600 on the server.

## Renewal and verification

`renew-nginx.sh` is installed as `/etc/letsencrypt/renewal-hooks/deploy/20-barsan-nginx-reload`, mode 0750. It checks Nginx syntax before reloading and records a constant non-secret journal message. Certbot's packaged `certbot.timer` must be enabled and active; no duplicate cron job is added.

Run `certbot renew --cert-name experiment.barsun4818.ir --dry-run --run-deploy-hooks --non-interactive` and verify both exit success and a matching `barsan-tls-renewal` journal event. Certbot can add a randomized delay; allow time for that. The ACME test CA used by this simulation is external certificate infrastructure, not an application Staging deployment; it does not replace the live certificate.

Run `python3 ops/ubuntu-production/verify_tls.py --expected-status 503` during this infrastructure step. After deploying the application, run without that override; the default requires the real app and database health. It validates trusted certificate/hostname verification, redirects preserving synthetic query parameters, and TLS 1.2 and 1.3 independently. Never use insecure TLS flags to obtain a pass.

Access logging omits query strings, referrers, cookies and authorization headers. The domain error log is restricted to critical errors. Before admitting research participants, include application/upstream error-log privacy in the final deployment audit; these HTTP/TLS checks alone are not that audit.

## Recovery and operation records

The protected audit directory stores step records, package/Certbot output, original UFW files and Nginx configuration backups. Raw protected operation logs stay on the server. Never commit or export the Let's Encrypt account/private-key directories.

Nginx configuration is tested before every reload. If a configuration test fails during setup, restore the captured known-good HTTP configuration and retain the issued certificate for inspection/reuse. On renewal-hook failure, inspect the protected Certbot log and Nginx configuration, correct the configuration, then reload; do not repeatedly force new production issuance.

Preserve the existing SSH session until a fresh login succeeds after firewall changes. The original UFW state was inactive with SSH 22 allowed; backups preserve that baseline. Do not overwrite other firewall rules or change sshd as a side effect of TLS setup.

App/database migration, operational SSH hardening, off-server backup restore drills, deployment automation and production synthetic capacity acceptance remain subsequent steps. No application Staging service or database is permitted on this server.
