"""Issue only after recorded DNS/port verification; never print private material."""
from pathlib import Path
from datetime import datetime,timezone
import subprocess,json,os,shutil,urllib.request
root=Path('/var/lib/barsan-bootstrap/2026-11-05-tls')
proof=json.loads((root/'network-verified.json').read_text())
assert proof['domain']=='experiment.barsun4818.ir' and proof['resolved_ipv4']==['37.152.183.1']
assert all(proof[x] for x in ['external_http_80_challenge','external_tcp_443','fresh_ssh_after_firewall','dns_authoritative_verified'])
assert (datetime.now(timezone.utc)-datetime.fromisoformat(proof['verified_at_utc'])).total_seconds()<1800
conf=Path('/etc/nginx/sites-available/barsan-production')
record=json.loads((root/'record.json').read_text())
assert record['status']=='awaiting_external_network_verification'
assert not Path('/etc/letsencrypt/live/experiment.barsun4818.ir').exists()
def save():
 (root/'record.json').write_text(json.dumps(record,indent=2));os.chmod(root/'record.json',0o600)
def run(args,label,timeout=180):
 p=subprocess.run(args,capture_output=True,text=True,timeout=timeout)
 path=root/(label+'.log');path.write_text(p.stdout+p.stderr);os.chmod(path,0o600)
 if p.returncode:raise RuntimeError('TLS operation failed: '+label)
 return p.stdout
try:
 # Validate local HTTP serving and CA reachability again immediately before issuance.
 with urllib.request.urlopen('http://experiment.barsun4818.ir/.well-known/acme-challenge/barsan-preflight',timeout=10) as response:
  assert response.read().decode().strip()==proof['marker']
 with urllib.request.urlopen('https://acme-v02.api.letsencrypt.org/directory',timeout=10) as response:assert json.load(response).get('newOrder')
 run(['certbot','certonly','--webroot','--webroot-path','/var/www/barsan-acme','--domain','experiment.barsun4818.ir','--cert-name','experiment.barsun4818.ir','--non-interactive','--agree-tos','--register-unsafely-without-email','--key-type','ecdsa'],'certificate-issuance',240)
 record['certificate_issued']=True;record['account_email']='unset';record['steps'].append('Issued production Let\u2019s Encrypt certificate only after DNS/network gate');save()
 chain=Path('/etc/letsencrypt/live/experiment.barsun4818.ir/fullchain.pem');key=chain.parent/'privkey.pem'
 assert chain.is_file() and key.is_file()
 run(['systemctl','stop','barsan-tls-network-probe'],'stop-network-probe')
 shutil.copy2(conf,root/'backup/nginx-http-before-tls')
 http='''server {
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
    location / { return 308 https://experiment.barsun4818.ir$request_uri; }
}
server {
    listen 443 ssl http2;
    server_name experiment.barsun4818.ir;
    server_tokens off;
    ssl_certificate /etc/letsencrypt/live/experiment.barsun4818.ir/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/experiment.barsun4818.ir/privkey.pem;
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_ciphers ECDHE-ECDSA-AES128-GCM-SHA256:ECDHE-RSA-AES128-GCM-SHA256:ECDHE-ECDSA-CHACHA20-POLY1305:ECDHE-RSA-CHACHA20-POLY1305:ECDHE-ECDSA-AES256-GCM-SHA384:ECDHE-RSA-AES256-GCM-SHA384;
    ssl_prefer_server_ciphers off;
    ssl_session_cache shared:barsanTLS:10m;
    ssl_session_timeout 1d;
    ssl_session_tickets off;
    access_log /var/log/nginx/barsan-production.access.log barsan_privacy;
    error_log /var/log/nginx/barsan-production.error.log crit;
    location = / { return 302 /dce/$is_args$args; }
    location = /dce { return 308 /dce/$is_args$args; }
    location /dce/ {
        default_type text/plain;
        add_header Retry-After 300 always;
        return 503 "Application deployment is not complete.\\n";
    }
    location / { return 404; }
}
'''
 conf.write_text(http)
 try:run(['nginx','-t'],'nginx-tls-config');run(['systemctl','reload','nginx'],'nginx-tls-reload')
 except Exception:
  shutil.copy2(root/'backup/nginx-http-before-tls',conf)
  run(['nginx','-t'],'nginx-rollback-test');run(['systemctl','reload','nginx'],'nginx-rollback-reload');raise
 hook=Path('/etc/letsencrypt/renewal-hooks/deploy/20-barsan-nginx-reload')
 assert not hook.exists()
 hook.write_text('#!/bin/sh\nset -eu\n/usr/sbin/nginx -t\n/bin/systemctl reload nginx\n/usr/bin/logger -t barsan-tls-renewal "Nginx configuration verified and reloaded after certificate renewal"\n');os.chmod(hook,0o750)
 run(['systemctl','enable','--now','certbot.timer'],'renewal-timer')
 record['steps'].append('Configured HTTP-to-HTTPS redirects, TLS 1.2/1.3, domain certificate, renewal timer and Nginx reload hook');record['status']='https_configured_renewal_verification_pending';save()
 # All ACME test material remains in Certbot; this creates no application staging.
 run(['certbot','renew','--cert-name','experiment.barsun4818.ir','--dry-run','--run-deploy-hooks','--non-interactive'],'renewal-dry-run',300)
 record.update(status='tls_and_renewal_verified',renewal_dry_run_passed=True,renewal_hook_tested=True,completed_at_utc=datetime.now(timezone.utc).isoformat(),application_ready=False)
 record['steps'].append('Simulated renewal succeeded, including the deploy hook; live certificate retained');save()
 metadata=run(['openssl','x509','-in',str(chain),'-noout','-issuer','-subject','-dates','-ext','subjectAltName'],'public-certificate-metadata')
 print(json.dumps({'certificate_issued':True,'renewal_dry_run_passed':True,'renewal_hook_tested':True,'application_ready':False,'public_certificate_metadata':metadata.splitlines()}),flush=True)
except Exception as exc:
 record.update(status='incomplete_requires_review',error_type=type(exc).__name__);save();raise SystemExit('TLS setup requires review; inspect protected operation log')
