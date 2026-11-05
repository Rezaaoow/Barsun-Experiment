"""Create a dedicated operations login; do not change sshd before external proof."""
import argparse,base64,hashlib,json,os,pwd,shutil,subprocess
from pathlib import Path
from datetime import datetime,timezone
p=argparse.ArgumentParser();p.add_argument('--public-key',required=True);p.add_argument('--expected-fingerprint',required=True);args=p.parse_args()
assert os.geteuid()==0
parts=args.public_key.strip().split();assert parts[0]=='ssh-ed25519' and len(parts)>=2
blob=base64.b64decode(parts[1],validate=True)
fingerprint='SHA256:'+base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip('=')
assert fingerprint==args.expected_fingerprint
user='barsan-ops';home=Path('/home')/user;job=Path('/var/lib/barsan-bootstrap/2026-11-05-operations')
try:pwd.getpwnam(user)
except KeyError:pass
else:raise SystemExit('Existing operations account must be inspected before changes')
assert not home.exists() and not job.exists() and not Path('/etc/sudoers.d/50-barsan-ops').exists()
job.mkdir(mode=0o700);backup=job/'backup';backup.mkdir(mode=0o700)
for source,target in [('/etc/ssh/sshd_config','sshd_config'),('/etc/ssh/sshd_config.d','sshd_config.d'),('/etc/sudoers','sudoers'),('/etc/sudoers.d','sudoers.d')]:
 src=Path(source)
 if src.is_dir():shutil.copytree(src,backup/target,symlinks=True)
 else:shutil.copy2(src,backup/target)
root_keys=Path('/root/.ssh/authorized_keys').read_bytes()
root_shadow=next(x for x in Path('/etc/shadow').read_text().splitlines() if x.startswith('root:'))
r={'started_at_utc':datetime.now(timezone.utc).isoformat(),'account':user,'public_key_fingerprint':fingerprint,'steps':[],'existing_root_keys_sha256':hashlib.sha256(root_keys).hexdigest(),'status':'preparing'}
def save():
 (job/'record.json').write_text(json.dumps(r,indent=2));os.chmod(job/'record.json',0o600)
def run(command):
 p=subprocess.run(command,capture_output=True,text=True,timeout=30)
 if p.returncode:raise RuntimeError('Operations preparation failed: '+Path(command[0]).name)
 return p.stdout.strip()
save()
try:
 run(['useradd','--create-home','--user-group','--shell','/bin/bash','--comment','Barsan production operations',user])
 account=pwd.getpwnam(user);os.chmod(home,0o750)
 ssh=home/'.ssh';ssh.mkdir(mode=0o700);os.chown(ssh,account.pw_uid,account.pw_gid)
 keyfile=ssh/'authorized_keys'
 keyfile.write_text('no-agent-forwarding,no-X11-forwarding '+args.public_key.strip()+'\n')
 os.chmod(keyfile,0o600);os.chown(keyfile,account.pw_uid,account.pw_gid)
 r['steps'].append('Created password-locked operations account using existing authorized Mac public key');save()
 rule=job/'sudoers-candidate';rule.write_text('barsan-ops ALL=(ALL:ALL) NOPASSWD: ALL\n');os.chmod(rule,0o440)
 run(['visudo','-cf',str(rule)])
 target=Path('/etc/sudoers.d/50-barsan-ops');shutil.copy2(rule,target);os.chmod(target,0o440)
 try:run(['visudo','-c'])
 except Exception:target.unlink();raise
 assert run(['passwd','-S',user]).split()[1]=='L'
 assert run(['runuser','-u',user,'--','sudo','-n','id','-u'])=='0'
 assert Path('/root/.ssh/authorized_keys').read_bytes()==root_keys
 assert next(x for x in Path('/etc/shadow').read_text().splitlines() if x.startswith('root:'))==root_shadow
 r.update(status='awaiting_external_operations_login',password_locked=True,sudo_noninteractive_verified=True,root_credentials_unchanged=True,sshd_unchanged=True)
 r['steps'].append('Validated dedicated sudo rule; preserved root credentials and sshd configuration');save()
 print(json.dumps({k:v for k,v in r.items() if k!='steps'}))
except Exception as exc:
 r.update(status='incomplete_requires_inspection',error_type=type(exc).__name__);save();raise SystemExit('Operations preparation stopped; inspect protected record')
