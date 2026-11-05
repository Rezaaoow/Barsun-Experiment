"""Operator-side fresh SSH verification. Never copies private keys to the server."""
import argparse,json,re,shlex,subprocess
from pathlib import Path
p=argparse.ArgumentParser();p.add_argument('--identity',type=Path,default=Path.home()/'.ssh/id_ed25519_server');p.add_argument('--host',default='37.152.183.1');args=p.parse_args()
assert args.identity.is_file()
base=['ssh','-o','BatchMode=yes','-o','IdentitiesOnly=yes','-o','StrictHostKeyChecking=yes','-o','ConnectTimeout=10','-o','ControlMaster=no','-o','ControlPath=none','-i',str(args.identity)]
def ssh(user,command,extra=None):
    r=subprocess.run(base+(extra or [])+[user+'@'+args.host,command],capture_output=True,text=True,timeout=30)
    return r
root=ssh('root','id -u');assert root.returncode==0 and root.stdout.strip()=='0','Fresh root-key recovery login failed'
probe="import os,json,subprocess; assert os.getuid()!=0; user=subprocess.check_output(['id','-un'],text=True).strip(); uid=subprocess.check_output(['sudo','-n','id','-u'],text=True).strip(); assert user=='barsan-ops' and uid=='0'; print(json.dumps({'operations_login':True,'sudo':True}))"
ops=ssh('barsan-ops','python3 -c '+shlex.quote(probe));assert ops.returncode==0,'Fresh operations/sudo login failed'
assert json.loads(ops.stdout)=={'operations_login':True,'sudo':True}
negative=ssh('barsan-ops','true',['-vv','-o','PubkeyAuthentication=no','-o','PreferredAuthentications=password,keyboard-interactive','-o','NumberOfPasswordPrompts=0'])
methods=re.findall(r'Authentications that can continue: ([^\r\n]+)',negative.stderr)
assert negative.returncode!=0 and methods and methods[-1].split(',')==['publickey'],'Server must advertise publickey only; no password is sent by this probe'
finalize=r'''
from pathlib import Path
from datetime import datetime,timezone
import subprocess,json,hashlib,os
job=Path('/var/lib/barsan-bootstrap/2026-11-05-operations');path=job/'record.json';r=json.loads(path.read_text())
assert r['status'] in ['ssh_hardening_awaiting_fresh_validation','operations_and_key_only_ssh_verified']
config=Path('/etc/ssh/sshd_config.d/00-barsan-key-auth.conf');assert hashlib.sha256(config.read_bytes()).hexdigest()==r['dropin_sha256']
assert hashlib.sha256(Path('/root/.ssh/authorized_keys').read_bytes()).hexdigest()==r['existing_root_keys_sha256']
for old in (job/'backup/sshd_config.d').glob('*.conf'):assert old.read_bytes()==(Path('/etc/ssh/sshd_config.d')/old.name).read_bytes()
assert (job/'backup/sshd_config').read_bytes()==Path('/etc/ssh/sshd_config').read_bytes()
compile((job/'rollback-ssh.py').read_text(),'rollback-ssh.py','exec')
assert subprocess.run(['sshd','-t'],capture_output=True).returncode==0
if r['status']=='ssh_hardening_awaiting_fresh_validation':
    assert subprocess.run(['systemctl','stop','barsan-ssh-safety-rollback.timer'],capture_output=True).returncode==0
    r.update(status='operations_and_key_only_ssh_verified',rollback_timer_armed=False,fresh_operations_login_after_reload=True,fresh_root_key_recovery_after_reload=True,non_key_authentication_advertised=False,advertised_authentication_methods=['publickey'],provider_configurations_unchanged=True,root_authorized_keys_unchanged=True,verified_at_utc=datetime.now(timezone.utc).isoformat())
    r['steps'].append('Versioned operator verifier checked fresh operations/root logins and publickey-only advertisement, then cancelled rollback')
    path.write_text(json.dumps(r,indent=2));os.chmod(path,0o600)
assert subprocess.run(['systemctl','is-active','barsan-ssh-safety-rollback.timer'],capture_output=True,text=True).stdout.strip()!='active'
print(json.dumps({'operations_login_verified':True,'sudo_verified':True,'root_key_recovery_verified':True,'key_only_ssh_verified':True,'rollback_timer_inactive':True,'provider_files_preserved':True}))
'''
result=ssh('barsan-ops','sudo -n python3 -c '+shlex.quote(finalize));assert result.returncode==0,'Final SSH validation failed; leave rollback armed and inspect protected record'
print(json.dumps(json.loads(result.stdout)))
