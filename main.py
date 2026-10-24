import csv
import io
import json
import os
import secrets
import sqlite3
import string
import uuid
from contextlib import contextmanager
from functools import lru_cache
from datetime import datetime, timezone
from typing import Any, Iterator

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

ROOT = os.path.dirname(os.path.abspath(__file__))
DATABASE_URL = os.getenv("DATABASE_URL", "").replace("postgres://", "postgresql://", 1)
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "")

# PostgreSQL connections are pooled and bounded per application process. Keep the
# pool small on shared hosting; the total possible connections is pool size times
# the number of Passenger processes.
DB_POOL_MIN_SIZE = max(0, int(os.getenv("DB_POOL_MIN_SIZE", "1")))
DB_POOL_MAX_SIZE = max(1, int(os.getenv("DB_POOL_MAX_SIZE", "4")))
DB_POOL_TIMEOUT = max(0.5, float(os.getenv("DB_POOL_TIMEOUT", "5")))
DB_POOL_MAX_WAITING = max(1, int(os.getenv("DB_POOL_MAX_WAITING", "32")))
SQLITE_BUSY_TIMEOUT_MS = max(1000, int(os.getenv("SQLITE_BUSY_TIMEOUT_MS", "10000")))

app = FastAPI(title="LifeTrade Research API", version="0.2.0")


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def code() -> str:
    alphabet = string.ascii_uppercase + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(8))


if DATABASE_URL:
    import psycopg
    from psycopg.rows import dict_row
    from psycopg.types.json import Jsonb
    from psycopg_pool import ConnectionPool, PoolTimeout

    DB_POOL = ConnectionPool(
        DATABASE_URL,
        min_size=min(DB_POOL_MIN_SIZE, DB_POOL_MAX_SIZE),
        max_size=DB_POOL_MAX_SIZE,
        timeout=DB_POOL_TIMEOUT,
        max_waiting=DB_POOL_MAX_WAITING,
        num_workers=1,
        kwargs={
            "row_factory": dict_row,
            "connect_timeout": max(1, int(os.getenv("DB_CONNECT_TIMEOUT", "5"))),
            "application_name": "lifetrade-api",
        },
        open=True,
    )

    @contextmanager
    def conn() -> Iterator[Any]:
        try:
            with DB_POOL.connection(timeout=DB_POOL_TIMEOUT) as connection:
                yield connection
        except PoolTimeout as exc:
            # Fail fast under saturation instead of allowing unbounded waiters.
            raise HTTPException(
                status_code=503,
                detail="Database is temporarily busy; please retry.",
                headers={"Retry-After": "2"},
            ) from exc

    def init_db() -> None:
        with conn() as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS sessions("
                "session_id TEXT PRIMARY KEY, "
                "participant_code TEXT UNIQUE NOT NULL, "
                "created_at TEXT NOT NULL, completed_at TEXT, "
                "status TEXT NOT NULL DEFAULT 'started', "
                "metadata JSONB NOT NULL DEFAULT '{}'::jsonb, "
                "trial_count INTEGER NOT NULL DEFAULT 0)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS trials("
                "client_event_id TEXT PRIMARY KEY, "
                "session_id TEXT NOT NULL REFERENCES sessions(session_id), "
                "received_at TEXT NOT NULL, row_data JSONB NOT NULL)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_trials_session_id ON trials(session_id)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_trials_received_at ON trials(received_at)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_sessions_created_at ON sessions(created_at)"
            )

else:
    DB_PATH = os.getenv("SQLITE_PATH", os.path.join(ROOT, "lifetrade.sqlite3"))
    os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)

    @contextmanager
    def conn() -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(
            DB_PATH,
            timeout=SQLITE_BUSY_TIMEOUT_MS / 1000,
            cached_statements=128,
        )
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute(f"PRAGMA busy_timeout = {SQLITE_BUSY_TIMEOUT_MS}")
            connection.execute("PRAGMA synchronous = NORMAL")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def init_db() -> None:
        with conn() as connection:
            # WAL allows readers to continue while a writer commits. SQLite still
            # serializes writes, so PostgreSQL is recommended for larger studies.
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS sessions("
                "session_id TEXT PRIMARY KEY, "
                "participant_code TEXT UNIQUE NOT NULL, "
                "created_at TEXT NOT NULL, completed_at TEXT, "
                "status TEXT NOT NULL DEFAULT 'started', "
                "metadata TEXT NOT NULL DEFAULT '{}', "
                "trial_count INTEGER NOT NULL DEFAULT 0)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS trials("
                "client_event_id TEXT PRIMARY KEY, "
                "session_id TEXT NOT NULL REFERENCES sessions(session_id), "
                "received_at TEXT NOT NULL, row_data TEXT NOT NULL)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_trials_session_id ON trials(session_id)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_trials_received_at ON trials(received_at)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_sessions_created_at ON sessions(created_at)"
            )


init_db()


class SessionIn(BaseModel):
    experiment_version: str = "online-mvp-0.1"
    user_agent: str | None = None
    screen_width: int | None = None
    screen_height: int | None = None


class TrialRecord(BaseModel):
    client_event_id: str = Field(min_length=1, max_length=100)
    row: dict[str, Any]


class TrialIn(BaseModel):
    session_id: str = Field(min_length=1, max_length=100)
    client_event_id: str = Field(min_length=1, max_length=100)
    row: dict[str, Any]


class TrialBatchIn(BaseModel):
    session_id: str = Field(min_length=1, max_length=100)
    trials: list[TrialRecord] = Field(min_length=1, max_length=25)


class CompleteIn(BaseModel):
    session_id: str = Field(min_length=1, max_length=100)
    status: str = "completed"
    trial_count: int = 0


@app.get("/api/health")
def health():
    database_type = "postgresql" if DATABASE_URL else "sqlite"
    try:
        with conn() as connection:
            connection.execute("SELECT 1").fetchone()
        return {
            "ok": True,
            "service": "lifetrade",
            "database": database_type,
            "database_connected": True,
            "time": now(),
        }
    except HTTPException:
        raise
    except Exception as exc:
        # Do not expose DSNs, credentials, or raw database error details.
        print(f"ERROR health check: {type(exc).__name__}")
        raise HTTPException(
            status_code=503,
            detail={
                "ok": False,
                "service": "lifetrade",
                "database": database_type,
                "database_connected": False,
                "time": now(),
            },
        ) from exc


@app.post("/api/session")
def create_session(data: SessionIn):
    sid = str(uuid.uuid4())
    participant = code()
    metadata = {
        "experiment_version": data.experiment_version,
        "user_agent": data.user_agent,
        "screen_width": data.screen_width,
        "screen_height": data.screen_height,
    }
    try:
        with conn() as connection:
            if DATABASE_URL:
                connection.execute(
                    "INSERT INTO sessions(session_id,participant_code,created_at,metadata) "
                    "VALUES(%s,%s,%s,%s)",
                    (sid, participant, now(), Jsonb(metadata)),
                )
            else:
                connection.execute(
                    "INSERT INTO sessions(session_id,participant_code,created_at,metadata) "
                    "VALUES(?,?,?,?)",
                    (sid, participant, now(), json.dumps(metadata, ensure_ascii=False)),
                )
    except HTTPException:
        raise
    except Exception as exc:
        # Log only exception class; participant metadata and database details are private.
        print(f"ERROR creating session: {type(exc).__name__}")
        raise HTTPException(500, "Could not create session") from exc
    return {"session_id": sid, "participant_code": participant}


def _persist_trials(session_id: str, records: list[TrialRecord]) -> tuple[int, int]:
    """Insert a bounded batch atomically and increment the stored count only for new events."""
    if not records:
        return 0, 0

    received_times = [now() for _ in records]
    if DATABASE_URL:
        values = [
            (record.client_event_id, session_id, received, Jsonb(record.row))
            for record, received in zip(records, received_times)
        ]
        marker = "%s"
        conflict_sql = " ON CONFLICT(client_event_id) DO NOTHING"
    else:
        values = [
            (
                record.client_event_id,
                session_id,
                received,
                json.dumps(record.row, ensure_ascii=False, separators=(",", ":")),
            )
            for record, received in zip(records, received_times)
        ]
        marker = "?"
        conflict_sql = ""

    value_sql = ",".join(["(" + ",".join([marker] * 4) + ")"] * len(values))
    params = tuple(value for row in values for value in row)

    with conn() as connection:
        if not DATABASE_URL:
            # Acquire SQLite's single-writer lock before reading, preventing a
            # concurrent read-to-write transaction upgrade from failing mid-batch.
            connection.execute("BEGIN IMMEDIATE")

        if DATABASE_URL:
            exists = connection.execute(
                "SELECT 1 FROM sessions WHERE session_id=%s", (session_id,)
            ).fetchone()
        else:
            exists = connection.execute(
                "SELECT 1 FROM sessions WHERE session_id=?", (session_id,)
            ).fetchone()
        if not exists:
            raise HTTPException(404, "Unknown session")

        if DATABASE_URL:
            sql = (
                "INSERT INTO trials(client_event_id,session_id,received_at,row_data) VALUES "
                + value_sql
                + conflict_sql
            )
        else:
            sql = (
                "INSERT OR IGNORE INTO trials(client_event_id,session_id,received_at,row_data) VALUES "
                + value_sql
            )

        cursor = connection.execute(sql, params)
        inserted = max(0, cursor.rowcount or 0)
        duplicates = len(records) - inserted

        if inserted:
            if DATABASE_URL:
                connection.execute(
                    "UPDATE sessions SET trial_count=trial_count+%s WHERE session_id=%s",
                    (inserted, session_id),
                )
            else:
                connection.execute(
                    "UPDATE sessions SET trial_count=trial_count+? WHERE session_id=?",
                    (inserted, session_id),
                )

    return inserted, duplicates


@app.post("/api/trial")
def save_trial(data: TrialIn):
    # Retain the original single-trial endpoint for compatibility with old clients.
    _persist_trials(
        data.session_id,
        [TrialRecord(client_event_id=data.client_event_id, row=data.row)],
    )
    return {"saved": True, "client_event_id": data.client_event_id}


@app.post("/api/trials")
def save_trials(data: TrialBatchIn):
    inserted, duplicates = _persist_trials(data.session_id, data.trials)
    return {
        "saved": True,
        "received_count": len(data.trials),
        "inserted_count": inserted,
        "duplicate_count": duplicates,
    }


@app.post("/api/complete")
def complete(data: CompleteIn):
    status = data.status if data.status in ("completed", "stopped", "error") else "completed"
    with conn() as connection:
        if DATABASE_URL:
            result = connection.execute(
                "UPDATE sessions SET status=%s,completed_at=%s WHERE session_id=%s "
                "RETURNING session_id",
                (status, now(), data.session_id),
            ).fetchone()
        else:
            result = connection.execute(
                "UPDATE sessions SET status=?,completed_at=? WHERE session_id=?",
                (status, now(), data.session_id),
            ).rowcount
    if not result:
        raise HTTPException(404, "Unknown session")
    return {"saved": True, "status": status}


def authorize(token: str | None) -> None:
    if not ADMIN_TOKEN or not secrets.compare_digest(token or "", ADMIN_TOKEN):
        raise HTTPException(401, "Unauthorized")


@app.get("/api/admin/sessions")
def sessions(x_admin_token: str | None = Header(default=None)):
    authorize(x_admin_token)
    with conn() as connection:
        rows = connection.execute(
            "SELECT session_id,participant_code,created_at,completed_at,status,trial_count "
            "FROM sessions ORDER BY created_at DESC"
        ).fetchall()
    return [dict(row) for row in rows]


@app.get("/api/admin/export.csv")
def export_csv(x_admin_token: str | None = Header(default=None)):
    authorize(x_admin_token)
    # Discover the union of dynamic JSON keys without loading all trial rows into RAM.
    with conn() as connection:
        if DATABASE_URL:
            key_rows = connection.execute(
                "SELECT DISTINCT json_key FROM trials "
                "CROSS JOIN LATERAL jsonb_object_keys(row_data) AS x(json_key)"
            ).fetchall()
        else:
            key_rows = connection.execute(
                "SELECT DISTINCT json_each.key AS json_key "
                "FROM trials, json_each(trials.row_data)"
            ).fetchall()
    fields = sorted({row["json_key"] for row in key_rows} | {"server_received_at", "server_session_id"})

    def generate_csv():
        buffer = io.StringIO(newline="")
        writer = csv.DictWriter(buffer, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        yield buffer.getvalue()
        buffer.seek(0)
        buffer.truncate(0)

        last_received_at = None
        last_event_id = None
        while True:
            # Keyset pagination bounds memory and closes the DB connection before
            # yielding a chunk to the WSGI response iterator.
            if last_received_at is None:
                query = (
                    "SELECT client_event_id,session_id,received_at,row_data FROM trials "
                    "ORDER BY received_at,client_event_id LIMIT 500"
                )
                params = ()
            elif DATABASE_URL:
                query = (
                    "SELECT client_event_id,session_id,received_at,row_data FROM trials "
                    "WHERE (received_at,client_event_id) > (%s,%s) "
                    "ORDER BY received_at,client_event_id LIMIT 500"
                )
                params = (last_received_at, last_event_id)
            else:
                query = (
                    "SELECT client_event_id,session_id,received_at,row_data FROM trials "
                    "WHERE (received_at,client_event_id) > (?,?) "
                    "ORDER BY received_at,client_event_id LIMIT 500"
                )
                params = (last_received_at, last_event_id)

            with conn() as connection:
                rows = connection.execute(query, params).fetchall()
            if not rows:
                break

            last_received_at = rows[-1]["received_at"]
            last_event_id = rows[-1]["client_event_id"]
            flattened = []
            for row in rows:
                item = json.loads(row["row_data"]) if isinstance(row["row_data"], str) else dict(row["row_data"])
                item["server_received_at"] = row["received_at"]
                item["server_session_id"] = row["session_id"]
                flattened.append(item)
            writer.writerows(flattened)
            yield buffer.getvalue()
            buffer.seek(0)
            buffer.truncate(0)

    return StreamingResponse(
        generate_csv(),
        media_type="text/csv",
        headers={
            "Content-Disposition": 'attachment; filename="lifetrade_trials.csv"',
            "Cache-Control": "no-store",
        },
    )

app.mount("/Stimuli", StaticFiles(directory=os.path.join(ROOT, "app", "Stimuli")), name="stimuli")


@lru_cache(maxsize=1)
def load_homepage() -> str:
    with open(os.path.join(ROOT, "app", "index.html"), encoding="utf-8") as file:
        return file.read()


@app.get("/", response_class=HTMLResponse)
def home():
    return load_homepage()
