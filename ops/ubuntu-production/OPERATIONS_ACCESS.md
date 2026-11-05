# Verified operations access — 2026-11-05

The Ubuntu Production server now has three distinct roles:

- `barsan-ops`: interactive/automation operations, using the operator's existing Mac SSH key, with explicitly configured noninteractive sudo for server administration. This is a privileged operator identity, not an application runtime identity.
- `barsan-deploy`: password-locked repository reader, owns the private checkout and server-only read-only GitHub deploy key; no sudo or inbound authorized key was granted.
- `root`: existing public-key recovery access retained. Existing provider/operator authorized keys and root credentials are unchanged. Root/password SSH is disabled; this does not change console passwords.

The `barsan-ops` account uses a password-locked local account, mode-0750 home, mode-0700 `.ssh`, and mode-0600 authorized_keys. Only the already-authorized Mac Ed25519 public key was installed; no private key was generated or copied to the server. Agent and X11 forwarding are disabled for this operations key. The sudo rule is root-owned mode 0440 and checked with `visudo` before installation. The operator key already had root access before this change; application/repository accounts do not receive the operator's privileges.

## Safe sequence

1. Inspect current users, sudo policy, SSH include ordering, effective settings, existing root key fingerprints, provider intent, firewall and recovery access.
2. `bootstrap_operations.py` requires the public key and expected fingerprint as explicit arguments. It stops on existing named account/home/sudo rule/audit directory. It backs up SSH/sudo configuration in a root-only audit directory and creates only the new operations account/key and sudo rule. Existing credentials remain unchanged. This is a bounded one-time bootstrap, not a rerunnable account replacement script.
3. Test a fresh external SSH connection as `barsan-ops` and `sudo -n id -u`. Only after success record `external_operations_login_verified` with its timestamp in the protected operation record.
4. `harden_ssh.py` requires that recent external proof. It adds `00-barsan-key-auth.conf` before the provider's `01-` and cloud-init's `50-/60-` files. Existing provider files remain byte-identical. Test syntax before reload. A five-minute systemd rollback timer is armed before the reload and removes only this owned drop-in if validation is not completed.
5. Verify fresh external operations/sudo and root-key connections after reload. Probe offered methods without sending a password: only `publickey` is advertised. Check effective settings, preserve root keys and provider files, then stop the rollback timer. Never cancel the timer merely because the original authenticated session remains connected.
6. Verify `barsan-deploy` still has no sudo, SSH/Nginx/renewal timer remain active, and public TLS/redirect checks still pass. No application or research-data change is part of this step.

## Key-only policy

The tracked `00-barsan-key-auth.conf` enables public keys and sets PasswordAuthentication/KbdInteractiveAuthentication/PermitEmptyPasswords to no, AuthenticationMethods to publickey, and PermitRootLogin to prohibit-password. The latter retains existing root keys as recovery while removing root password login over SSH. `UsePAM`, SSH port, forwarding for other users, provider files, host keys, root authorized_keys and root credentials are not rewritten.

The provider's original root-login file explicitly enabled passwords before Ubuntu's cloud-init defaults. The new early drop-in intentionally overrides those effective password settings without overwriting the provider-managed files. Effective settings were inspected, not inferred from file names. A provider-console recovery login and actual reboot have not yet been tested; do not claim those checks are complete.

## Records and recovery

Protected server audit: `/var/lib/barsan-bootstrap/2026-11-05-operations/record.json` and `backup/`. The generated root-only `rollback-ssh.py` is retained for recovery; it checks the owned drop-in's checksum before removal and validates syntax before reloading. It refuses to remove a file altered by another operator. Once post-reload fresh connections pass, the transient safety timer is cancelled; no recurring rollback is left active.

For manual recovery, use the tested root-key login or the verified operations login plus sudo. Inspect the protected record and configuration first; run the saved rollback only if restoring the previous password policy is actually intended. A provider console remains an additional recovery option to verify later, not a mechanism assumed tested here.

Routine login uses the existing Mac private key with `ssh -o IdentitiesOnly=yes -i ~/.ssh/id_ed25519_server barsan-ops@37.152.183.1`. No new password needs to be shared.

## Scope still pending

No application service/database was installed or migrated. The HTTPS endpoint `/dce/` remains intentional maintenance 503, not a ready experiment. Firewall/TLS from PR #36 remain active. Production-only data/key migration, backups/restore drill, complete pinned deployment automation, monitoring and isolated synthetic Production capacity acceptance are later steps. No application Staging environment is permitted.

`verify_operations.py` runs on the operator workstation with the existing private-key file (default `~/.ssh/id_ed25519_server`). It establishes fresh root and operations connections without multiplexing, verifies noninteractive sudo, inspects advertised methods without sending a password, checks configuration/provider integrity and rollback-script syntax, and only then cancels the safety timer. It can also recheck the already-verified setup without reapplying hardening. Failed validation leaves the rollback armed. Do not copy the workstation private key to the server to run this operator-side verifier.

## Old-host preservation requirement

The existing hosting deployment must remain intact and fully operational throughout migration. All configuration/install/reload/restart/firewall/testing actions in this directory target only 37.152.183.1 and experiment.barsun4818.ir. Do not deploy these changes to cPanel, run the legacy cPanel release scripts, alter the old domains/redirects/DNS, stop old services, close old links or mutate old research records. Future data import, if needed, may use only a consistent read-only snapshot with controlled resource use; it must not require an old-host maintenance window or stop ongoing collection. Old and new databases remain independent; a snapshot is not continuous synchronization. No cutover or retirement of the old system is implied or authorized.
