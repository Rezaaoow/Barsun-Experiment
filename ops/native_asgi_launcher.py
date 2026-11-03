"""Start the UT/DD resilience staging runtime with no secrets in command arguments."""
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
EXPECTED_FILES = {'barsan_experiment.py': 'b1353278f7e95c676004246598b667ccec04363be02ec3611ff0e89b7c85adea', 'request_metrics.py': '53a447b0e1bf299283ef3296748abd22679a72b1291722d0e6c3b19e4dcbf046', 'task_records.py': 'b9e735eafc8ae3e7e16c9ef16b4085959972829404e188ea8cb15d3d59ceb6ad', 'app/index.html': '7f9fe17b1ebbaeeb351ba620184b24598bf12ff010590850737a9349951aa449', 'app/persistence.js': '83d1769c1c97e5fcf147a1f52a3705fd51f8e081c9d559cf9d13d94380981416', 'app/admin-links.html': '152f93010cc928ab118ceeb7b77eac93d398cf697aca4e9f738a12c8725d5d4c', 'app/fonts/vazirmatn-v33.003.woff2': '4e3fa217d38fdafc1fea4414ceb58ca5e662cf0ab5fa735a8c8c20e8b42cad92'}
for relative_path, expected_sha in EXPECTED_FILES.items():
    assert hashlib.sha256((ROOT / "repo" / relative_path).read_bytes()).hexdigest() == expected_sha, relative_path
env['BARSAN_REQUEST_METRICS'] = '1'
env['BARSAN_SYNC_THREADS'] = '8'
os.chdir(ROOT / 'repo')
os.execve(PYTHON, [
    PYTHON, '-m', 'uvicorn', 'barsan_experiment:app',
    '--host', '127.0.0.1', '--port', '8766', '--workers', '2',
    '--root-path', '/lifetrade', '--proxy-headers',
    '--forwarded-allow-ips', '127.0.0.1', '--no-access-log',
    '--header', 'X-Barsan-Runtime:uvicorn-pr25-native',
], env)
