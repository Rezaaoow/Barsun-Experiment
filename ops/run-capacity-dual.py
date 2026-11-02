"""Run k6 off-host, guarding the unchanged production health endpoint."""
from pathlib import Path
import json
import os
import signal
import subprocess
import sys
import time

label, count = sys.argv[1], int(sys.argv[2])
assert count in [100,200,300,400,600]
assert label=='200-dual-mac' and count==200
root = Path('/Users/rezaaoow/barsan-loadtest/native-asgi-20261102')
os.umask(0o077)
fixture = root / f'participants-{label}.json'
assert fixture.exists(), 'Transfer the approved synthetic fixture privately before running'
assert not (root/f'{label}-summary.json').exists(), 'cohort has already been run'
(root/f'{label}-start.json').write_text(json.dumps({'started_at':time.time(),'users':count,'release':'7af183881410faf5d089efefe44d71d43781d8ce'}))
with open(root/f'{label}.log','w') as output:
    child = subprocess.Popen(['/opt/homebrew/bin/k6','run','--quiet','--summary-trend-stats','avg,min,med,max,p(90),p(95),p(99)','--summary-export',str(root/f'{label}-summary.json'),'-e','BASE_URL=https://staging.my-experiment.ir/lifetrade','-e','PARTICIPANTS_FILE='+str(fixture),'-e','EXPECTED_USERS='+str(count),'-e','LOAD_PROFILE=ramp',str(root/'k6-capacity-fix.js')],stdout=output,stderr=subprocess.STDOUT)
    (root/f'{label}.pid').write_text(str(child.pid))
    print('LOAD_STARTED',label,child.pid,flush=True)
    failures = slow = 0
    with open(root/f'production-guard-{label}.jsonl','a',buffering=1) as guard:
        while child.poll() is None:
            start = time.monotonic()
            result = subprocess.run(['curl','-sS','--max-time','10','https://my-experiment.ir/lifetrade/api/health'],capture_output=True)
            elapsed = time.monotonic()-start
            try:
                health = json.loads(result.stdout)
                ok = result.returncode == 0 and health.get('ok') is True and health.get('database_connected') is True
            except Exception:
                ok = False
            failures = 0 if ok else failures+1
            slow = slow+1 if elapsed>5 else 0
            guard.write(json.dumps({'time':time.time(),'ok':ok,'seconds':elapsed,'curl_exit':result.returncode,'curl_error':result.stderr.decode(errors='replace').strip()})+'\n')
            if failures>=2 or slow>=3:
                child.send_signal(signal.SIGINT)
                print('STOPPED_BY_PRODUCTION_GUARD',label,flush=True)
                break
            time.sleep(10)
    exit_code = child.wait()
    (root/f'{label}-end.json').write_text(json.dumps({'finished_at':time.time(),'exit_code':exit_code}))
    print('LOAD_FINISHED',label,'exit_code',exit_code,flush=True)
