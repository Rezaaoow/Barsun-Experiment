"""Prepare HTTP validation and a temporary 443 reachability probe; no issuance."""
from pathlib import Path
import subprocess,json,os,shutil,uuid,hashlib
from datetime import datetime,timezone
assert os.geteuid()==0
job=Path('/var/lib/barsan-bootstrap/2026-11-05-tls')
assert not job.exists() and not Path('/etc/nginx').exists() and not Path('/etc/letsencrypt').exists()
job.mkdir(mode=0o700);backup=job/'backup';backup.mkdir(mode=0o700)
shutil.copytree('/etc/ufw',backup/'ufw')
record={'started_at_utc':datetime.now(timezone.utc).isoformat(),'domain':'experiment.barsun4818.ir','canonical_url':'https://experiment.barsun4818.ir/dce/','certificate_issued':False,'application_deployed':False,'steps':[]}
def save():
 (job/'record.json').write_text(json.dumps(record,indent=2));os.chmod(job/'record.json',0o600)
def run(args,timeout=180):
 p=subprocess.run(args,capture_output=True,text=True,timeout=timeout)
 name=Path(args[0]).name
 if p.returncode:
  f=job/('failed-'+name+'.log');f.write_text(p.stdout+p.stderr);os.chmod(f,0o600)
  raise RuntimeError('Command failed: '+name)
 return p.stdout.strip()
save()
try:
 run(['apt-get','update'],300);record['steps'].append('Updated signed distribution package indexes');save()
 env=os.environ.copy();env['DEBIAN_FRONTEND']='noninteractive'
 p=subprocess.run(['apt-get','install','-y','--no-install-recommends','nginx','certbot'],env=env,capture_output=True,text=True,timeout=300)
 (job/'package-install.log').write_text(p.stdout+p.stderr);os.chmod(job/'package-install.log',0o600)
 assert p.returncode==0
 record['steps'].append('Installed distribution Nginx and Certbot packages');save()
 shutil.copytree('/etc/nginx',backup/'nginx-after-package-install',symlinks=True)
 # Only the new package's default-site symlink is disabled. No custom site existed.
 default=Path('/etc/nginx/sites-enabled/default')
 if default.exists() or default.is_symlink():
  assert default.is_symlink() and default.resolve()==Path('/etc/nginx/sites-available/default')
  default.unlink()
 webroot=Path('/var/www/barsan-acme');assert not webroot.exists()
 challenge=webroot/'.well-known/acme-challenge';challenge.mkdir(parents=True,mode=0o755)
 marker='barsan-network-'+uuid.uuid4().hex
 (challenge/'barsan-preflight').write_text(marker+'\n')
 logformat=Path('/etc/nginx/conf.d/10-barsan-access-log.conf');assert not logformat.exists()
 logformat.write_text('log_format barsan_privacy escape=json \'{"time":"$time_iso8601","method":"$request_method","uri":"$uri","status":$status,"bytes":$body_bytes_sent,"seconds":$request_time}\';\n')
 conf=Path('/etc/nginx/sites-available/barsan-production');assert not conf.exists()
 conf.write_text('''server {
    listen 80 default_server;
    server_name _;
    access_log off;
    return 444;
}
server {
    listen 80;
    server_name experiment.barsun4818.ir;
    server_tokens off;
    access_log /var/log/nginx/barsan-production.access.log barsan_privacy;
    error_log /var/log/nginx/barsan-production.error.log crit;
    location ^~ /.well-known/acme-challenge/ {
        root /var/www/barsan-acme;
        default_type text/plain;
        try_files $uri =404;
    }
    location / {
        default_type text/plain;
        add_header Retry-After 300 always;
        return 503 "Application deployment is not complete.\\n";
    }
}
''')
 Path('/etc/nginx/sites-enabled/barsan-production').symlink_to(conf)
 run(['nginx','-t']);run(['systemctl','enable','--now','nginx']);run(['systemctl','reload','nginx'])
 record['steps'].append('Configured domain-only HTTP challenge webroot and maintenance responses; no app traffic');save()
 # Preserve the existing SSH allowance and add only the requested web ports.
 for port in ['22/tcp','80/tcp','443/tcp']:run(['ufw','allow',port])
 run(['ufw','--force','enable'])
 record['steps'].append('Enabled UFW with SSH 22 and web 80/443 allowed; previous UFW files backed up');save()
 probe=job/'port-probe.py'
 probe.write_text('''import socket
from pathlib import Path
marker=(Path(__file__).parent/'network-marker').read_bytes()
s=socket.socket();s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1);s.bind(('0.0.0.0',443));s.listen(16)
while True:
 c,a=s.accept()
 with c:c.sendall(marker)
''')
 os.chmod(probe,0o600);(job/'network-marker').write_text(marker+'\n');os.chmod(job/'network-marker',0o600)
 run(['systemd-run','--unit=barsan-tls-network-probe','--property=RuntimeMaxSec=600','--property=NoNewPrivileges=yes','/usr/bin/python3',str(probe)])
 record.update(status='awaiting_external_network_verification',network_marker=marker,packages=run(['dpkg-query','-W','-f=${Package} ${Version}\n','nginx','certbot']).splitlines())
 save();print(json.dumps({'status':record['status'],'network_marker':marker,'certificate_issued':False,'nginx_config_valid':True,'firewall_enabled':True,'audit_directory':str(job)}),flush=True)
except Exception as exc:
 record.update(status='incomplete_requires_review',error_type=type(exc).__name__);save();raise SystemExit('TLS HTTP preparation stopped; inspect protected audit record')
