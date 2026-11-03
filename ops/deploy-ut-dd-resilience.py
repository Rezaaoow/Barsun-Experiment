"""One-shot, hash-checked Staging deployment; no secrets leave the host."""
from pathlib import Path
import hashlib
import json
import os
import shutil
import subprocess
import tarfile
import time
import urllib.request
from datetime import datetime, timezone

ROOT = Path('/home/myexperi/lifetrade-staging')
JOB = ROOT / 'deployments/ut-dd-resilience-20261103-68d22169'
COMMIT = '68d22169d8d599d4fe6bc2034873c6a58ab7f593'
BUNDLE_SHA = '275b6aa7788d72d383cfc5dd1e857493621d72bb7436bfee6df036162e383cbc'
CTL = ['/home/myexperi/virtualenv/lifetrade-staging/3.12/bin/supervisorctl', '-c',
       str(ROOT / 'native-asgi-trial-20261102/supervisor.ini')]
PYTHON = '/home/myexperi/virtualenv/lifetrade-staging/3.12/bin/python'


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def control(action, check=True):
    return subprocess.run(CTL + [action, 'barsan-native'], capture_output=True, timeout=45, check=check)


def health(url):
    with urllib.request.urlopen(url, timeout=5) as response:
        body = json.load(response)
        return response.status == 200 and body.get('database_connected') is True


def ready():
    successes = 0
    for _ in range(45):
        try:
            healthy = health('http://127.0.0.1:8766/api/health')
        except Exception:
            healthy = False
        successes = successes + 1 if healthy else 0
        if successes >= 3:
            return
        time.sleep(1)
    raise RuntimeError('Bounded readiness checks failed')


def install(source, destination, mode=0o644):
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + '.68d22169.new')
    assert not temporary.exists()
    shutil.copy2(source, temporary)
    os.chmod(temporary, mode)
    os.replace(temporary, destination)


assert ROOT.name == 'lifetrade-staging'
assert digest(JOB / 'runtime-bundle.tar.gz') == BUNDLE_SHA
assert (JOB / 'staging-before.dump').stat().st_size > 0
before = json.loads((JOB / 'backup-manifest.json').read_text())
assert digest(ROOT / 'repo/barsan_experiment.py') == before['repo/barsan_experiment.py']
assert json.loads((ROOT / 'current-release.json').read_text())['commit_sha'] == '7af183881410faf5d089efefe44d71d43781d8ce'
assert not (JOB / 'deployment-result.json').exists(), 'Inspect an earlier deployment result before resuming'
candidate = JOB / 'candidate'
candidate.mkdir(mode=0o700)
with tarfile.open(JOB / 'runtime-bundle.tar.gz') as archive:
    for member in archive.getmembers():
        assert member.isfile() and not member.issym() and not member.islnk()
        assert (candidate / member.name).resolve().is_relative_to(candidate.resolve())
        archive.extract(member, candidate, filter='data')
manifest = json.loads((candidate / 'runtime-manifest.json').read_text())
assert manifest['source_commit'] == COMMIT
expected = manifest['files']
for name, expected_sha in expected.items():
    assert digest(candidate / 'runtime' / name) == expected_sha, name
for name in ['barsan_experiment.py', 'task_records.py', 'request_metrics.py']:
    subprocess.run([PYTHON, '-m', 'py_compile', str(candidate / 'runtime' / name)], check=True, capture_output=True)
production_paths = [Path('/home/myexperi/lifetrade/repo/barsan_experiment.py'),
                    Path('/home/myexperi/lifetrade/repo/app/index.html'),
                    Path('/home/myexperi/public_html/lifetrade/.htaccess')]
production_before = {str(path): digest(path) for path in production_paths}
assert health('https://staging.my-experiment.ir/lifetrade/api/health')
assert health('https://my-experiment.ir/lifetrade/api/health')
stopped = False
phase = 'stop'
result = {'source_commit': COMMIT, 'production_hashes_before': production_before}
started_at = time.monotonic()
try:
    control('stop')
    stopped = True
    phase = 'install'
    for name in [*expected, *manifest['additional_files']]:
        install(candidate / 'runtime' / name, ROOT / 'repo' / name)
    install(candidate / 'launch_native.py', ROOT / 'native-asgi-trial-20261102/launch_native.py', 0o600)
    phase = 'start'
    control('start')
    ready()
    phase = 'public-verification'
    for _ in range(3):
        assert health('https://staging.my-experiment.ir/lifetrade/api/health')
    with urllib.request.urlopen('https://staging.my-experiment.ir/lifetrade/', timeout=15) as response:
        html = response.read().decode()
        assert response.status == 200
        assert 'barsan-task-outbox-v1' in html and 'async function completeTaskDurably' in html
        assert '/lifetrade/assets/vazirmatn-v33.003.woff2' in html
    with urllib.request.urlopen('https://staging.my-experiment.ir/lifetrade/assets/vazirmatn-v33.003.woff2', timeout=15) as response:
        assert hashlib.sha256(response.read()).hexdigest() == expected['app/fonts/vazirmatn-v33.003.woff2']
    assert all(digest(ROOT / 'repo' / name) == value for name, value in expected.items())
    assert all(digest(path) == production_before[str(path)] for path in production_paths)
    assert health('https://my-experiment.ir/lifetrade/api/health')
    release = json.loads((ROOT / 'current-release.json').read_text())
    release.update(commit_sha=COMMIT, previous_commit_sha=release['commit_sha'],
                   deployed_at=datetime.now(timezone.utc).isoformat(),
                   runtime_mode='uvicorn-via-litespeed-loopback-proxy', runtime_files=expected,
                   source_branch='codex/staging-capacity-fix-20261103')
    temporary = ROOT / 'current-release.68d22169.new'
    temporary.write_text(json.dumps(release, indent=2) + '\n')
    os.chmod(temporary, 0o600)
    os.replace(temporary, ROOT / 'current-release.json')
    result.update(deployed=True, public_health=True, html_verified=True, font_verified=True,
                  production_unchanged=True, duration_seconds=round(time.monotonic()-started_at, 3))
except Exception as error:
    result.update(deployed=False, failed_phase=phase, error_type=type(error).__name__)
    try:
        if stopped:
            control('stop', check=False)
            for name, old_sha in before.items():
                destination = ROOT / name
                if old_sha is not None:
                    assert digest(JOB / 'backup' / name) == old_sha
                    shutil.copy2(JOB / 'backup' / name, destination)
                elif destination.exists():
                    relative = name.removeprefix('repo/')
                    assert relative in expected or relative in manifest['additional_files']
                    assert digest(destination) == digest(candidate / 'runtime' / relative)
                    destination.unlink()
        control('start', check=False)
        ready()
        result['rollback_healthy'] = health('https://staging.my-experiment.ir/lifetrade/api/health')
    except Exception as rollback_error:
        result['rollback_error_type'] = type(rollback_error).__name__
    (JOB / 'deployment-result.json').write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result))
    raise SystemExit(2)
(JOB / 'deployment-result.json').write_text(json.dumps(result, indent=2) + '\n')
print(json.dumps(result))
