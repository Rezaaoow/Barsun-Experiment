"""Frozen existing-native update. No environment, routing or dependency changes."""
import ast
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import time
import urllib.request
import psycopg
from psycopg.conninfo import conninfo_to_dict

os.umask(0o077)
environment, commit, bundle_hash = sys.argv[1:]
assert environment in ('staging', 'production') and len(commit) == 40
staging = environment == 'staging'
root = Path('/home/myexperi/lifetrade-staging' if staging else '/home/myexperi/lifetrade')
native = root / ('native-asgi-trial-20261102' if staging else 'native-asgi')
job = root / 'deployments' / ('modules-' + commit[:12])
source = job / 'runtime'
python = Path('/home/myexperi/virtualenv') / root.name / '3.12/bin/python'
ctl = [str(python.parent / 'supervisorctl'), '-c', str(native / 'supervisor.ini')]
public = 'https://staging.my-experiment.ir/lifetrade' if staging else 'https://my-experiment.ir/lifetrade'
old = json.loads((root / 'current-release.json').read_text())
assert not source.exists() and old['runtime_mode'] == 'uvicorn-via-litespeed-loopback-proxy'
sha = lambda path: hashlib.sha256(path.read_bytes()).hexdigest()
assert sha(job / 'bundle.tar.gz') == bundle_hash
source.mkdir(mode=0o700)
with tarfile.open(job / 'bundle.tar.gz') as archive:
    for member in archive.getmembers():
        assert member.isfile() and not member.issym() and not member.islnk()
        assert (source / member.name).resolve().is_relative_to(source.resolve())
        archive.extract(member, source, filter='data')
manifest = json.loads((source / 'runtime-manifest.json').read_text())
expected = manifest['files']
assert manifest['source_commit'] == commit and len(expected) == 217
assert set(expected) == set(old['runtime_files'])
for name, checksum in expected.items():
    if not (source / name).exists():
        previous = Path(old['runtime_source']) / name
        assert sha(previous) == checksum
        target = source / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(previous, target)
    assert sha(source / name) == checksum
if not staging:
    accepted = json.loads(Path('/home/myexperi/lifetrade-staging/module-acceptance.json').read_text())
    assert accepted['passed'] and accepted['commit_sha'] == commit and accepted['runtime_files'] == expected
    stage = json.loads(Path('/home/myexperi/lifetrade-staging/current-release.json').read_text())
    assert stage['commit_sha'] == commit and stage['runtime_files'] == expected
values = {}
for line in (root / 'config/app.env').read_text().splitlines():
    if line.strip() and not line.lstrip().startswith('#') and '=' in line:
        key, value = line.split('=', 1); values[key.strip()] = value.strip()
info = conninfo_to_dict(values['DATABASE_URL'])
assert info['dbname'] == ('myexperi_barsan_staging' if staging else 'myexperi_lifetrade_study')
backup = job / 'backup'; backup.mkdir(mode=0o700)
for name, path in [('launcher', native / 'launch_native.py'), ('supervisor', native / 'supervisor.ini'),
                   ('release', root / 'current-release.json'), ('env', root / 'config/app.env')]:
    shutil.copy2(path, backup / name)
pgenv = os.environ.copy()
for field, key in [('host', 'PGHOST'), ('port', 'PGPORT'), ('dbname', 'PGDATABASE'), ('user', 'PGUSER'), ('password', 'PGPASSWORD')]:
    if field in info: pgenv[key] = info[field]
def run(command, **kwargs):
    result = subprocess.run(command, capture_output=True, timeout=120, **kwargs)
    if result.returncode: raise RuntimeError('Private host operation failed')
    return result
run(['pg_dump', '-Fc', '--no-owner', '--no-acl', '-f', str(job / 'database-before.dump')], env=pgenv)
run(['pg_restore', '--list', str(job / 'database-before.dump')])
assert (job / 'database-before.dump').stat().st_size > 0
launcher = (backup / 'launcher').read_text()
old_source = repr(old['runtime_source'])
assert launcher.count(old_source) == 1
launcher = launcher.replace(old_source, repr(str(source)))
# Replace only the verified inline file manifest, never secret environment values.
match = []
for node in ast.walk(ast.parse(launcher)):
    if isinstance(node, ast.Dict):
        try: value = ast.literal_eval(node)
        except Exception: continue
        if value == old['runtime_files']: match.append(ast.get_source_segment(launcher, node))
assert len(match) == 1 and launcher.count(match[0]) == 1
launcher = launcher.replace(match[0], repr(expected))
compile(launcher, 'launch_native.py', 'exec')
def atomic(path, content):
    temporary = path.with_name(path.name + '.module-new')
    assert not temporary.exists(); temporary.write_bytes(content); os.chmod(temporary, 0o600); os.replace(temporary, path)
def health():
    with urllib.request.urlopen(public + '/api/health', timeout=10) as response:
        body = json.load(response)
        return response.status == 200 and body.get('database_connected') is True and body.get('database') == 'postgresql'
def ready():
    consecutive = 0
    for _ in range(40):
        try: ok = health()
        except Exception: ok = False
        consecutive = consecutive + 1 if ok else 0
        if consecutive == 3: return
        time.sleep(1)
    raise RuntimeError('Public readiness failed')
changed = False
mirrored = False
try:
    # Additive registry initialization uses this environment's existing database.
    runtime_env = os.environ.copy(); runtime_env.update(values)
    run([str(python), '-c', 'import barsan_experiment'], cwd=source, env=runtime_env)
    run(ctl + ['stop', 'barsan-native'])
    changed = True
    atomic(native / 'launch_native.py', launcher.encode())
    run(ctl + ['start', 'barsan-native']); ready()
    with urllib.request.urlopen(public + '/', timeout=15) as response:
        html = response.read().decode()
        assert 'function startIntermission()' in html and "step.module_code==='dd_standalone'" in html
    with urllib.request.urlopen(public + '/admin/links', timeout=15) as response:
        html = response.read().decode()
        assert 'data-add-module="dd_standalone"' in html and 'data-add-module="intermission"' in html
    release = dict(old, commit_sha=commit, runtime_source=str(source), runtime_files=expected)
    atomic(root / 'current-release.json', json.dumps(release, indent=2).encode())
    mirrored = True
    for name in expected:
        target = root / 'repo' / name
        if not target.is_file() or sha(target) != expected[name]:
            target.parent.mkdir(parents=True, exist_ok=True); shutil.copy2(source / name, target)
    assert (root / 'config/app.env').read_bytes() == (backup / 'env').read_bytes()
    result = dict(deployed=True, environment=environment, commit_sha=commit, public_health=True,
                  runtime_files=expected, runtime_source=str(source), runtime_file_count=len(expected),
                  independent_database=True, environment_unchanged=True, backup_verified=True)
except Exception as error:
    rollback = False
    if changed:
        subprocess.run(ctl + ['stop', 'barsan-native'], capture_output=True, timeout=45)
        atomic(native / 'launch_native.py', (backup / 'launcher').read_bytes())
        atomic(root / 'current-release.json', (backup / 'release').read_bytes())
        run(ctl + ['start', 'barsan-native']); ready(); rollback = True
    if mirrored:
        for name in old['runtime_files']:
            shutil.copy2(Path(old['runtime_source']) / name, root / 'repo' / name)
    result = dict(deployed=False, error_type=type(error).__name__, rollback_verified=rollback)
    (job / 'result.json').write_text(json.dumps(result)); print(json.dumps(result)); sys.exit(2)
(job / 'result.json').write_text(json.dumps(result, indent=2))
print(json.dumps({k:v for k,v in result.items() if k not in ('runtime_files','runtime_source')}))
