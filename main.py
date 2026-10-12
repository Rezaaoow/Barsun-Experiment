import os, json, csv, io, uuid, secrets, string
from datetime import datetime, timezone
from fastapi import FastAPI, HTTPException, Header
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

ROOT = os.path.dirname(os.path.abspath(__file__))
DATABASE_URL = os.getenv('DATABASE_URL', '').replace('postgres://', 'postgresql://', 1)
ADMIN_TOKEN = os.getenv('ADMIN_TOKEN', '')
app = FastAPI(title='LifeTrade Research API', version='0.1.0')

def now(): return datetime.now(timezone.utc).isoformat()
def code(): return ''.join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(8))

if DATABASE_URL:
    import psycopg
    from psycopg.rows import dict_row
    from psycopg.types.json import Jsonb
    def conn(): return psycopg.connect(DATABASE_URL, row_factory=dict_row)
    def init_db():
        with conn() as c:
            c.execute("CREATE TABLE IF NOT EXISTS sessions(session_id TEXT PRIMARY KEY, participant_code TEXT UNIQUE NOT NULL, created_at TEXT NOT NULL, completed_at TEXT, status TEXT NOT NULL DEFAULT 'started', metadata JSONB NOT NULL DEFAULT '{}'::jsonb, trial_count INTEGER NOT NULL DEFAULT 0)")
            c.execute("CREATE TABLE IF NOT EXISTS trials(client_event_id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(session_id), received_at TEXT NOT NULL, row_data JSONB NOT NULL)")
else:
    import sqlite3
    DB_PATH = os.getenv('SQLITE_PATH', os.path.join(ROOT, 'lifetrade.sqlite3'))
    def conn():
        c = sqlite3.connect(DB_PATH, timeout=20)
        c.row_factory = sqlite3.Row
        c.execute('PRAGMA foreign_keys = ON')
        return c
    def init_db():
        with conn() as c:
            c.execute("CREATE TABLE IF NOT EXISTS sessions(session_id TEXT PRIMARY KEY, participant_code TEXT UNIQUE NOT NULL, created_at TEXT NOT NULL, completed_at TEXT, status TEXT NOT NULL DEFAULT 'started', metadata TEXT NOT NULL DEFAULT '{}', trial_count INTEGER NOT NULL DEFAULT 0)")
            c.execute("CREATE TABLE IF NOT EXISTS trials(client_event_id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(session_id), received_at TEXT NOT NULL, row_data TEXT NOT NULL)")
init_db()

class SessionIn(BaseModel):
    experiment_version: str = 'online-mvp-0.1'
    user_agent: str | None = None
    screen_width: int | None = None
    screen_height: int | None = None
class TrialIn(BaseModel):
    session_id: str
    client_event_id: str
    row: dict
class CompleteIn(BaseModel):
    session_id: str
    status: str = 'completed'
    trial_count: int = 0

@app.get('/api/health')
def health(): return {'ok': True, 'service': 'lifetrade', 'time': now()}


@app.post('/api/session')
def create_session(data: SessionIn):
    sid, participant = str(uuid.uuid4()), code()
    metadata = {
        'experiment_version': data.experiment_version,
        'user_agent': data.user_agent,
        'screen_width': data.screen_width,
        'screen_height': data.screen_height
    }

    try:
        with conn() as c:
            if DATABASE_URL:
                c.execute(
                    'INSERT INTO sessions(session_id,participant_code,created_at,metadata) VALUES(%s,%s,%s,%s)',
                    (sid, participant, now(), Jsonb(metadata))
                )
            else:
                c.execute(
                    'INSERT INTO sessions(session_id,participant_code,created_at,metadata) VALUES(?,?,?,?)',
                    (sid, participant, now(), json.dumps(metadata, ensure_ascii=False))
                )
    except Exception as e:
        print(f'ERROR creating session: {type(e).__name__}: {e}')
        raise HTTPException(500, 'Could not create session')

    return {'session_id': sid, 'participant_code': participant}


@app.post('/api/trial')
def save_trial(data: TrialIn):
    with conn() as c:
        if DATABASE_URL:
            exists = c.execute('SELECT session_id FROM sessions WHERE session_id=%s', (data.session_id,)).fetchone()
            if not exists: raise HTTPException(404, 'Unknown session')
            c.execute('INSERT INTO trials(client_event_id,session_id,received_at,row_data) VALUES(%s,%s,%s,%s) ON CONFLICT(client_event_id) DO NOTHING', (data.client_event_id, data.session_id, now(), Jsonb(data.row)))
            c.execute('UPDATE sessions SET trial_count=(SELECT COUNT(*) FROM trials WHERE session_id=%s) WHERE session_id=%s', (data.session_id, data.session_id))
        else:
            exists = c.execute('SELECT session_id FROM sessions WHERE session_id=?', (data.session_id,)).fetchone()
            if not exists: raise HTTPException(404, 'Unknown session')
            c.execute('INSERT OR IGNORE INTO trials(client_event_id,session_id,received_at,row_data) VALUES(?,?,?,?)', (data.client_event_id, data.session_id, now(), json.dumps(data.row, ensure_ascii=False)))
            c.execute('UPDATE sessions SET trial_count=(SELECT COUNT(*) FROM trials WHERE session_id=?) WHERE session_id=?', (data.session_id, data.session_id))
    return {'saved': True, 'client_event_id': data.client_event_id}

@app.post('/api/complete')
def complete(data: CompleteIn):
    status = data.status if data.status in ('completed', 'stopped', 'error') else 'completed'
    with conn() as c:
        if DATABASE_URL:
            result = c.execute('UPDATE sessions SET status=%s,completed_at=%s WHERE session_id=%s RETURNING session_id', (status, now(), data.session_id)).fetchone()
        else:
            result = c.execute('UPDATE sessions SET status=?,completed_at=? WHERE session_id=?', (status, now(), data.session_id)).rowcount
    if not result: raise HTTPException(404, 'Unknown session')
    return {'saved': True, 'status': status}

def authorize(token):
    if not ADMIN_TOKEN or not secrets.compare_digest(token or '', ADMIN_TOKEN): raise HTTPException(401, 'Unauthorized')

@app.get('/api/admin/sessions')
def sessions(x_admin_token: str | None = Header(default=None)):
    authorize(x_admin_token)
    with conn() as c:
        rows = c.execute('SELECT session_id,participant_code,created_at,completed_at,status,trial_count FROM sessions ORDER BY created_at DESC').fetchall()
    return [dict(r) for r in rows]

@app.get('/api/admin/export.csv')
def export_csv(x_admin_token: str | None = Header(default=None)):
    authorize(x_admin_token)
    with conn() as c: rows = c.execute('SELECT session_id,received_at,row_data FROM trials ORDER BY received_at').fetchall()
    flattened = []
    for r in rows:
        item = json.loads(r['row_data']) if isinstance(r['row_data'], str) else r['row_data']
        item['server_received_at'] = r['received_at']
        item['server_session_id'] = r['session_id']
        flattened.append(item)
    fields = sorted({k for row in flattened for k in row.keys()})
    out = io.StringIO(); writer = csv.DictWriter(out, fieldnames=fields, extrasaction='ignore'); writer.writeheader(); writer.writerows(flattened); out.seek(0)
    return StreamingResponse(iter([out.getvalue()]), media_type='text/csv', headers={'Content-Disposition': 'attachment; filename="lifetrade_trials.csv"'})

app.mount('/Stimuli', StaticFiles(directory=os.path.join(ROOT, 'app', 'Stimuli')), name='stimuli')
@app.get('/', response_class=HTMLResponse)
def home():
    with open(os.path.join(ROOT, 'app', 'index.html'), encoding='utf-8') as f: return f.read()
