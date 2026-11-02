"""Start the fixed PR25 staging runtime with no secrets in command arguments."""
from pathlib import Path
import hashlib
import os

ROOT = Path('/home/myexperi/lifetrade-staging')
PYTHON = '/home/myexperi/virtualenv/lifetrade-staging/3.12/bin/python'
env = os.environ.copy()
for raw in (ROOT / 'config/app.env').read_text().splitlines():
    line = raw.strip()
    if line and not line.startswith('#') and '=' in line:
        key, value = line.split('=', 1)
        env[key.strip()] = value.strip()
assert env.get('STUDY_CODE', '').startswith('barsan-staging-')
assert env.get('PUBLIC_EXPERIMENT_URL', '').rstrip('/') == 'https://staging.my-experiment.ir/lifetrade'
assert hashlib.sha256((ROOT / 'repo/barsan_experiment.py').read_bytes()).hexdigest() == '03e2c6704e3dd8b21ea19d46eca610a4e1ef14f7612d642b7432cd78e03dbf9e'
assert hashlib.sha256((ROOT / 'repo/request_metrics.py').read_bytes()).hexdigest() == '3b02fc4dc3633d5d5ef9a3088469a071bf936b3655ba2cd3941d93a955481191'
env['BARSAN_REQUEST_METRICS'] = '1'
os.chdir(ROOT / 'repo')
os.execve(PYTHON, [
    PYTHON, '-m', 'uvicorn', 'barsan_experiment:app',
    '--host', '127.0.0.1', '--port', '8766', '--workers', '2',
    '--root-path', '/lifetrade', '--proxy-headers',
    '--forwarded-allow-ips', '127.0.0.1', '--no-access-log',
    '--header', 'X-Barsan-Runtime:uvicorn-pr25-native',
], env)
