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
assert hashlib.sha256((ROOT / 'repo/barsan_experiment.py').read_bytes()).hexdigest() == '37a26b9e69cbd685ce5054eef03d174c5dea93500fe34326bfeccbc3f5919c4c'
os.chdir(ROOT / 'repo')
os.execve(PYTHON, [
    PYTHON, '-m', 'uvicorn', 'barsan_experiment:app',
    '--host', '127.0.0.1', '--port', '8766', '--workers', '2',
    '--root-path', '/lifetrade', '--proxy-headers',
    '--forwarded-allow-ips', '127.0.0.1', '--no-access-log',
    '--header', 'X-Barsan-Runtime:uvicorn-pr25-native',
], env)
