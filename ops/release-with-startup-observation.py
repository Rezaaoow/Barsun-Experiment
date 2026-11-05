"""One-time new-Production release using observed worker startup, no application tests.

Existing root release/backup/configuration guards remain intact. The user explicitly
requested no test runs; observe the actual workers' successful startup journal only.
"""
from pathlib import Path
import hashlib
path=Path('/var/lib/barsan-operations/registration-release-742b.py')
source=path.read_text()
assert hashlib.sha256(source.encode()).hexdigest()=="dea22f7510db0f3b58db53efa051923adceb6780330342b7e8fb1036ea1fb0f9"
start=source.index('def health(port):')
end=source.index('def route(body):',start)
startup="def health(port):\n service='barsan-registration-candidate.service' if port==8768 else 'barsan-production.service'\n for _ in range(40):\n  state=subprocess.run(['systemctl','is-active',service],capture_output=True,text=True).stdout.strip()\n  invocation=subprocess.run(['systemctl','show','-p','InvocationID','--value',service],capture_output=True,text=True).stdout.strip()\n  if state=='active' and invocation:\n   logs=subprocess.run(['journalctl','--namespace=barsan','-u',service,'_SYSTEMD_INVOCATION_ID='+invocation,'-o','cat','--no-pager'],capture_output=True,text=True).stdout\n   if logs.count('Application startup complete.')>=2:return\n  time.sleep(1)\n raise RuntimeError('workers_did_not_finish_startup')\n"
source=source[:start]+startup+source[end:]
exec(compile(source,str(path), 'exec'))
