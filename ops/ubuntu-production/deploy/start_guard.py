"""Fail closed before application import; no old-host/database fallback."""
from pathlib import Path
import os,json
from urllib.parse import urlsplit,parse_qs
assert os.getuid()!=0
url=urlsplit(os.environ['DATABASE_URL']);query=parse_qs(url.query)
assert url.scheme=='postgresql' and not url.netloc and url.path=='/barsan_production'
assert query=={'user':['barsan-app'],'host':['/var/run/postgresql']}
assert os.environ['PUBLIC_EXPERIMENT_URL']=='https://experiment.barsun4818.ir/dce/'
assert len(os.environ['ADMIN_TOKEN'])>=40
key=Path(os.environ['INVITATION_TOKEN_KEY_FILE']);assert key==Path('/etc/barsan/invitation.key') and key.is_file()
current=Path('/opt/barsan/current').resolve();release=json.loads(Path('/opt/barsan/current-release.json').read_text())
assert current==Path('/opt/barsan/releases')/release['commit']
assert current.stat().st_uid==0 and current.stat().st_mode & 0o022==0
assert (current/'.bulk-export.lock').is_symlink() and (current/'.bulk-export.lock').resolve()==Path('/var/lib/barsan/bulk-export.lock')
