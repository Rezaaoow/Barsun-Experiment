"""Fetch exact Linux wheels from official PyPI, verifying complete-file SHA-256.

The new server's CDN connection stalls on large responses. Bounded HTTP ranges
avoid that transport problem without a mirror or weaker TLS/hash verification.
Run with the destination Python venv so compatibility tags match its runtime.
"""
import argparse
import concurrent.futures
import gzip
import hashlib
import json
from pathlib import Path
import platform
import re
import time
import urllib.parse
import urllib.request
from pip._vendor.packaging.tags import sys_tags
from pip._vendor.packaging.utils import canonicalize_name, parse_wheel_filename
from pip._vendor.packaging.specifiers import SpecifierSet

CHUNK = 32768
TAGS = {tag: rank for rank, tag in enumerate(sys_tags())}

def request(url, headers, maximum):
    for attempt in range(3):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=20) as response:
                assert urllib.parse.urlsplit(response.url).hostname == urllib.parse.urlsplit(url).hostname
                body = response.read(maximum + 1)
                assert len(body) <= maximum
                return response.status, response.headers, body
        except Exception:
            if attempt == 2:
                raise
            time.sleep(0.5 * (2 ** attempt))

def package(name, version, destination):
    assert re.fullmatch(r'[a-z0-9-]+', name) and re.fullmatch(r'[0-9A-Za-z.+!-]+', version)
    status, headers, body = request('https://pypi.org/pypi/' + name + '/' + version + '/json', {'Accept-Encoding':'gzip'}, 4*1024*1024)
    assert status == 200
    data = json.loads(gzip.decompress(body) if headers.get('Content-Encoding') == 'gzip' else body)
    assert canonicalize_name(data['info']['name']) == name and data['info']['version'] == version
    candidates = []
    for release in data['urls']:
        if release['packagetype'] != 'bdist_wheel' or release.get('yanked'):
            continue
        if release.get('requires_python') and platform.python_version() not in SpecifierSet(release['requires_python']):
            continue
        wheel_name, wheel_version, _, tags = parse_wheel_filename(release['filename'])
        ranks = [TAGS[tag] for tag in tags if tag in TAGS]
        if canonicalize_name(wheel_name) == name and str(wheel_version) == version and ranks:
            candidates.append((min(ranks), release['filename'], release))
    assert candidates, 'No compatible official wheel: ' + name
    release = min(candidates, key=lambda item:(item[0],item[1]))[2]
    url = urllib.parse.urlsplit(release['url'])
    assert url.scheme == 'https' and url.hostname == 'files.pythonhosted.org' and not url.query and not url.fragment
    assert Path(release['filename']).name == release['filename']
    expected = release['digests']['sha256']
    assert re.fullmatch(r'[0-9a-f]{64}', expected)
    target = destination / release['filename']
    if target.exists():
        assert target.stat().st_size == release['size'] and hashlib.sha256(target.read_bytes()).hexdigest() == expected, 'Existing cache mismatch; inspect before replacing'
    else:
        temporary = destination / (release['filename'] + '.partial')
        assert not temporary.exists(), 'Inspect incomplete download before retrying'
        try:
            with temporary.open('xb') as output:
                for start in range(0, release['size'], CHUNK):
                    end = min(start + CHUNK, release['size']) - 1
                    status, headers, chunk = request(release['url'], {'Range':f'bytes={start}-{end}', 'Accept-Encoding':'identity'}, end-start+1)
                    assert status == 206 and headers.get('Content-Range') == f"bytes {start}-{end}/{release['size']}"
                    assert len(chunk) == end-start+1
                    output.write(chunk)
            assert temporary.stat().st_size == release['size']
            assert hashlib.sha256(temporary.read_bytes()).hexdigest() == expected, 'Official complete-file hash mismatch'
            temporary.chmod(0o644); temporary.rename(target)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
    print(json.dumps({'verified_package':name,'version':version,'bytes':release['size']}), flush=True)
    return name, {'version':version,'wheel':release['filename'],'sha256':expected,'bytes':release['size'],'source':release['url']}

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--versions', type=Path, required=True)
    parser.add_argument('--destination', type=Path, required=True)
    parser.add_argument('--lock', type=Path, required=True)
    parser.add_argument('--manifest', type=Path, required=True)
    args = parser.parse_args()
    assert platform.machine() == 'x86_64' and platform.python_version().startswith('3.12.')
    versions = json.loads(args.versions.read_text())
    assert isinstance(versions, dict) and versions
    args.destination.mkdir(mode=0o755, parents=True, exist_ok=True)
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(package, name, version, args.destination) for name,version in sorted(versions.items())]
        wheels = dict(future.result() for future in futures)
    assert {p.name for p in args.destination.iterdir()} == {wheel['wheel'] for wheel in wheels.values()}
    args.lock.write_text('# Ubuntu 24.04 / CPython 3.12 / amd64; official PyPI wheel hashes.\n' + ''.join(name+'=='+wheel['version']+' --hash=sha256:'+wheel['sha256']+'\n' for name,wheel in sorted(wheels.items())))
    args.lock.chmod(0o600)
    args.manifest.write_text(json.dumps(wheels, indent=2)); args.manifest.chmod(0o600)
    print(json.dumps({'verified_wheels':len(wheels),'transport':'official HTTPS ranges','complete_sha256_verified':True}), flush=True)

if __name__ == '__main__':
    main()
