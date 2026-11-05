"""Apply key-only SSH only after external operations login proof; timed rollback."""
from pathlib import Path
from datetime import datetime,timezone
import subprocess,json,hashlib,os
assert os.geteuid()==0
job=Path('/var/lib/barsan-bootstrap/2026-11-05-operations');path=job/'record.json';r=json.loads(path.read_text())
assert r['status']=='external_operations_login_verified'
assert (datetime.now(timezone.utc)-datetime.fromisoformat(r['external_verified_at_utc'])).total_seconds()<1800
config=Path('/etc/ssh/sshd_config.d/00-barsan-key-auth.conf');assert not config.exists()
content='''# Barsan Production: key-only SSH; keep provider files and root key recovery.
# This early drop-in wins over provider/cloud-init password-login defaults.
PubkeyAuthentication yes
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitEmptyPasswords no
AuthenticationMethods publickey
PermitRootLogin prohibit-password
'''
checksum=hashlib.sha256(content.encode()).hexdigest()
def run(args):
 p=subprocess.run(args,capture_output=True,text=True,timeout=30)
 if p.returncode:raise RuntimeError('SSH hardening command failed: '+Path(args[0]).name)
 return p.stdout.strip()
def save():
 path.write_text(json.dumps(r,indent=2));os.chmod(path,0o600)
assert run(['sshd','-t'])==''
assert hashlib.sha256(Path('/root/.ssh/authorized_keys').read_bytes()).hexdigest()==r['existing_root_keys_sha256']
rollback=job/'rollback-ssh.py';assert not rollback.exists()
rollback.write_text('''from pathlib import Path
import subprocess,json,hashlib,os
from datetime import datetime,timezone
job=Path('/var/lib/barsan-bootstrap/2026-11-05-operations')
config=Path('/etc/ssh/sshd_config.d/00-barsan-key-auth.conf')
expected=CHECKSUM
if config.exists():
 assert hashlib.sha256(config.read_bytes()).hexdigest()==expected,'Refusing to remove configuration changed by another operator'
 config.unlink()
 assert subprocess.run(['sshd','-t'],capture_output=True).returncode==0
 assert subprocess.run(['systemctl','reload','ssh'],capture_output=True).returncode==0
p=job/'record.json';r=json.loads(p.read_text());r.update(status='ssh_hardening_rolled_back',rolled_back_at_utc=datetime.now(timezone.utc).isoformat());p.write_text(json.dumps(r,indent=2));os.chmod(p,0o600)
'''.replace('CHECKSUM',repr(checksum)))
os.chmod(rollback,0o600)
try:
 config.write_text(content);os.chmod(config,0o644)
 run(['sshd','-t'])
 # Rollback remains armed until independent post-reload logins prove access.
 run(['systemd-run','--unit=barsan-ssh-safety-rollback','--on-active=5m','--timer-property=AccuracySec=1s','/usr/bin/python3',str(rollback)])
 r.update(status='ssh_hardening_awaiting_fresh_validation',rollback_timer_armed=True,dropin_sha256=checksum);save()
 run(['systemctl','reload','ssh'])
 actual=dict(line.split(' ',1) for line in run(['sshd','-T']).splitlines())
 for k,v in {'pubkeyauthentication':'yes','passwordauthentication':'no','kbdinteractiveauthentication':'no','permitemptypasswords':'no','authenticationmethods':'publickey'}.items():assert actual[k]==v
 assert actual['permitrootlogin'] in ['without-password','prohibit-password']
 r['sshd_effective']={k:actual[k] for k in ['port','pubkeyauthentication','passwordauthentication','kbdinteractiveauthentication','authenticationmethods','permitrootlogin']};save()
 print(json.dumps({'key_only_configuration_loaded':True,'root_key_recovery_retained':True,'rollback_timer_armed':True,'effective':r['sshd_effective']}))
except Exception:
 subprocess.run(['python3',str(rollback)],capture_output=True)
 subprocess.run(['systemctl','stop','barsan-ssh-safety-rollback.timer'],capture_output=True)
 raise SystemExit('SSH hardening failed; automatic rollback attempted; inspect protected record')
