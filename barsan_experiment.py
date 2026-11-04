import asyncio
import csv
import hashlib
import io
import json
import logging
import math
import os
import random
import secrets
import sqlite3
import threading
import uuid
import zipfile
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from functools import lru_cache
from typing import Any, Iterator, Literal

from fastapi import FastAPI, Header, HTTPException, Request
from cryptography.fernet import Fernet, InvalidToken
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from request_metrics import RequestMetricsMiddleware, observed_connection
from bulk_exports import BulkExportMiddleware
from client_timing import TimingCheckpoint, initialize as initialize_client_timing, save as save_client_timing
from task_records import COMMON_FIELDS, DD_FIELDS, UT_FIELDS, dd_record, ultimatum_record

ROOT = os.path.dirname(os.path.abspath(__file__))
DATABASE_URL = os.getenv("DATABASE_URL", "").replace("postgres://", "postgresql://", 1)
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "")
STUDY_CODE = os.getenv("STUDY_CODE", "main-study").strip() or "main-study"
STUDY_NAME = os.getenv("STUDY_NAME", "BarSan Experiment").strip() or "BarSan Experiment"
TEST_STUDY_PREFIX = "barsan-test-"
PUBLIC_EXPERIMENT_URL = os.getenv("PUBLIC_EXPERIMENT_URL", "").rstrip("?&")
INVITATION_TOKEN_KEY_FILE = os.getenv(
    "INVITATION_TOKEN_KEY_FILE", os.path.join(os.path.expanduser("~"), ".barsan_invitation_tokens.key")
)
INSTRUMENT_CODE = "lifetrade"
INSTRUMENT_NAME = "Life-Trade delay discounting"
STAGE_TO_INSTRUMENT = {
    "questionnaire": ("questionnaire", "barsan-questionnaire-v1", "Questionnaire"),
    "dd_lifetrade": ("lifetrade", "online-mvp-0.4-randomization-visibility-fa", "DD vs Life-Trade"),
    "ultimatum": ("ultimatum", "barsan-ultimatum-v1", "Ultimatum Game"),
    "intermission": ("intermission", "barsan-intermission-v1", "Between-game rest"),
}
STAGE_ORDER = ("questionnaire", "dd_lifetrade", "ultimatum")
ULTIMATUM_WORKFLOW_VARIANTS = {"ultimatum_frame": "Frame", "ultimatum_control": "Control"}
DD_WORKFLOW_DESIGNS = {
    "dd_lifetrade": {"task_keys": ["LT", "KT"], "magnitude_keys": None, "expected_trials": 30},
    "dd_standalone": {"task_keys": ["KT"], "magnitude_keys": ["100M"], "expected_trials": 5},
}
WORKFLOW_MODULE_CODES = frozenset((*STAGE_ORDER, *ULTIMATUM_WORKFLOW_VARIANTS, "dd_standalone", "intermission"))
DEFAULT_WORKFLOW = (
    {"step_id": "default-questionnaire", "module_code": "questionnaire"},
    {"step_id": "default-dd-lifetrade", "module_code": "dd_lifetrade"},
)
ULTIMATUM_TOTAL_POINTS = 10
ULTIMATUM_MAIN_BLOCK_TRIALS = 5
ULTIMATUM_MAIN_BLOCK_COUNT = 4
ULTIMATUM_MAIN_TRIALS = ULTIMATUM_MAIN_BLOCK_TRIALS * ULTIMATUM_MAIN_BLOCK_COUNT
# Provisional, auditable opponent models. These can be adjusted after protocol confirmation.
ULTIMATUM_AI_OFFER_NORMAL_MEAN = 5.0
ULTIMATUM_AI_OFFER_NORMAL_SD = 4.0
ULTIMATUM_BINOMIAL_N = 10
ULTIMATUM_BINOMIAL_ACCEPT_THRESHOLD = 5
ULTIMATUM_BLOCKS = {
    "proposer_agent": {"role": "proposer", "opponent_condition": "agent"},
    "responder_agent": {"role": "responder", "opponent_condition": "agent"},
    "proposer_coworker": {"role": "proposer", "opponent_condition": "coworker"},
    "responder_coworker": {"role": "responder", "opponent_condition": "coworker"},
}
ULTIMATUM_PRACTICE_BLOCKS = {
    "practice_proposer_agent": {"role": "proposer", "opponent_condition": "agent"},
    "practice_responder_coworker": {"role": "responder", "opponent_condition": "coworker"},
}
ULTIMATUM_PRE_RESOLVE_EVENT_FIELDS = frozenset({
    "proposer_screen_shown_at",
    "slider_first_changed_at",
    "slider_last_changed_at",
    "offer_submitted_at",
    "waiting_started_at",
    "waiting_ended_at",
    "responder_offer_shown_at",
    "participant_choice_at",
})
ULTIMATUM_POST_RESOLVE_EVENT_FIELDS = frozenset({
    "result_screen_shown_at",
    "next_trial_clicked_at",
    "advance_clicked_at",
})
QUESTIONNAIRE_QUESTION_CODES = tuple(
    [f"B{i}" for i in range(1, 6)]
    + [f"PJ{i}" for i in range(1, 8)]
    + [f"DJ{i}" for i in range(1, 5)]
    + [f"IJ{i}" for i in range(1, 5)]
    + [f"INJ{i}" for i in range(1, 6)]
)
CONSENT_VERSION = "barsan-consent-v1"

# Bounded PostgreSQL connections: total possible connections are pool size times
# the number of Passenger processes. Keep this conservative on shared hosting.
DB_POOL_MIN_SIZE = max(0, int(os.getenv("DB_POOL_MIN_SIZE", "1")))
DB_POOL_MAX_SIZE = max(1, int(os.getenv("DB_POOL_MAX_SIZE", "4")))
DB_POOL_TIMEOUT = max(0.5, float(os.getenv("DB_POOL_TIMEOUT", "5")))
DB_POOL_MAX_WAITING = max(1, int(os.getenv("DB_POOL_MAX_WAITING", "128")))
SQLITE_BUSY_TIMEOUT_MS = max(1000, int(os.getenv("SQLITE_BUSY_TIMEOUT_MS", "10000")))

app = FastAPI(title="BarSan Experiment Research API", version="0.4.0")
app.add_middleware(BulkExportMiddleware, lock_path=os.path.join(ROOT, ".bulk-export.lock"), token_getter=lambda: ADMIN_TOKEN, busy_checker=lambda: _recent_task_activity())
if os.getenv("BARSAN_REQUEST_METRICS", "0") == "1":
    app.add_middleware(RequestMetricsMiddleware)


@app.on_event("startup")
async def configure_sync_threads():
    """Bound sync handler threads when configured for a constrained host."""
    limit = int(os.getenv("BARSAN_SYNC_THREADS", "0"))
    if not 0 <= limit <= 128:
        raise ValueError("BARSAN_SYNC_THREADS must be between 0 and 128")
    if limit:
        from anyio.to_thread import current_default_thread_limiter
        current_default_thread_limiter().total_tokens = limit


def _invitation_fernet() -> Fernet:
    """Load the invite-token encryption key, creating a private per-install key if needed."""
    configured_key = os.getenv("INVITATION_TOKEN_ENCRYPTION_KEY", "").strip()
    if configured_key:
        key = configured_key.encode("ascii")
    else:
        key_path = INVITATION_TOKEN_KEY_FILE
        os.makedirs(os.path.dirname(os.path.abspath(key_path)), mode=0o700, exist_ok=True)
        try:
            with open(key_path, "rb") as key_file:
                key = key_file.read().strip()
        except FileNotFoundError:
            generated = Fernet.generate_key()
            try:
                descriptor = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(descriptor, "wb") as key_file:
                    key_file.write(generated)
                key = generated
            except FileExistsError:
                with open(key_path, "rb") as key_file:
                    key = key_file.read().strip()
        try:
            os.chmod(key_path, 0o600)
        except OSError:
            pass
    try:
        return Fernet(key)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("Invitation-token encryption key is invalid.") from exc


def _encrypt_invitation_token(token: str) -> str:
    return _invitation_fernet().encrypt(token.encode("utf-8")).decode("ascii")


def _decrypt_invitation_token(encrypted_token: str | None) -> str | None:
    if not encrypted_token:
        return None
    try:
        return _invitation_fernet().decrypt(encrypted_token.encode("ascii")).decode("utf-8")
    except (InvalidToken, UnicodeError, ValueError):
        # Legacy assignments only stored a one-way hash; key loss also requires reissue.
        return None


def _invitation_url_for_token(request: Request, api_route: str, token: str) -> str:
    base_url = _invitation_base_url(request, api_route)
    joiner = "&" if "?" in base_url else "?"
    return base_url + joiner + "invite=" + token


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_participant_code(value: str) -> str:
    # Existing participant codes are supplied by the research team/participant;
    # this function normalizes input only and never generates a participant code.
    normalized = value.strip().upper()
    if not normalized:
        raise HTTPException(422, "Participant code is required")
    return normalized


def _invitation_base_url(request: Request, api_route: str) -> str:
    """Use an explicit public URL when configured, otherwise derive the mounted app path."""
    if PUBLIC_EXPERIMENT_URL:
        return PUBLIC_EXPERIMENT_URL.rstrip("/")

    request_path = str(request.url.path or "")
    prefix = request_path[:-len(api_route)] if request_path.endswith(api_route) else ""
    root_path = str(request.scope.get("root_path") or "").rstrip("/")
    if not prefix:
        prefix = root_path
    base = str(request.base_url).rstrip("/")
    if prefix and not base.endswith(prefix.rstrip("/")):
        base += prefix if prefix.startswith("/") else "/" + prefix
    return base.rstrip("/")


if DATABASE_URL:
    from psycopg.rows import dict_row
    from psycopg.types.json import Jsonb
    from psycopg_pool import ConnectionPool, PoolTimeout, TooManyRequests

    def _build_db_pool() -> ConnectionPool:
        return ConnectionPool(
            DATABASE_URL,
            min_size=min(DB_POOL_MIN_SIZE, DB_POOL_MAX_SIZE),
            max_size=DB_POOL_MAX_SIZE,
            timeout=DB_POOL_TIMEOUT,
            max_waiting=DB_POOL_MAX_WAITING,
            num_workers=1,
            kwargs={
                "row_factory": dict_row,
                "connect_timeout": max(1, int(os.getenv("DB_CONNECT_TIMEOUT", "5"))),
                "application_name": "barsan-experiment-api",
            },
            open=True,
        )

    # Passenger/LSAPI may fork a preloaded application. A psycopg pool and its
    # sockets/worker thread must never be shared across those process boundaries.
    DB_POOL = _build_db_pool()
    _DB_POOL_PID = os.getpid()
    _DB_POOL_REBUILD_LOCK = threading.Lock()
    _INHERITED_DB_POOLS: list[ConnectionPool] = []

    def _reset_db_pool_after_fork() -> None:
        global DB_POOL, _DB_POOL_PID, _DB_POOL_REBUILD_LOCK
        # Keep the inherited pool referenced: its destructor waits for worker
        # threads which do not survive fork, delaying the first request.
        if DB_POOL is not None:
            _INHERITED_DB_POOLS.append(DB_POOL)
        DB_POOL = None
        _DB_POOL_PID = -1
        _DB_POOL_REBUILD_LOCK = threading.Lock()

    if hasattr(os, "register_at_fork"):
        os.register_at_fork(after_in_child=_reset_db_pool_after_fork)

    def _ensure_db_pool_for_process() -> None:
        global DB_POOL, _DB_POOL_PID
        current_pid = os.getpid()
        if _DB_POOL_PID == current_pid:
            return
        with _DB_POOL_REBUILD_LOCK:
            if _DB_POOL_PID != current_pid:
                if DB_POOL is not None:
                    _INHERITED_DB_POOLS.append(DB_POOL)
                DB_POOL = _build_db_pool()
                _DB_POOL_PID = current_pid


    @contextmanager
    def conn() -> Iterator[Any]:
        _ensure_db_pool_for_process()
        try:
            with observed_connection(DB_POOL.connection(timeout=DB_POOL_TIMEOUT)) as connection:
                yield connection
        except (PoolTimeout, TooManyRequests) as exc:
            raise HTTPException(
                status_code=503,
                detail="Database is temporarily busy; please retry.",
                headers={"Retry-After": "2"},
            ) from exc

    def init_db() -> None:
        with conn() as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS studies ("
                "study_code TEXT PRIMARY KEY, study_name TEXT NOT NULL, created_at TEXT NOT NULL)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS participants ("
                "participant_id TEXT PRIMARY KEY, participant_code TEXT UNIQUE NOT NULL, "
                "created_at TEXT NOT NULL)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS study_participations ("
                "participation_id TEXT PRIMARY KEY, study_code TEXT NOT NULL REFERENCES studies(study_code), "
                "participant_id TEXT NOT NULL REFERENCES participants(participant_id), "
                "joined_at TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'active', "
                "UNIQUE(study_code, participant_id))"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS instruments ("
                "instrument_code TEXT PRIMARY KEY, instrument_name TEXT NOT NULL, created_at TEXT NOT NULL)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS instrument_versions ("
                "instrument_version_id TEXT PRIMARY KEY, instrument_code TEXT NOT NULL "
                "REFERENCES instruments(instrument_code), version TEXT NOT NULL, settings JSONB NOT NULL "
                "DEFAULT '{}'::jsonb, created_at TEXT NOT NULL, UNIQUE(instrument_code, version))"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS instrument_runs ("
                "run_id TEXT PRIMARY KEY, participation_id TEXT NOT NULL "
                "REFERENCES study_participations(participation_id), instrument_version_id TEXT NOT NULL "
                "REFERENCES instrument_versions(instrument_version_id), started_at TEXT NOT NULL, "
                "completed_at TEXT, status TEXT NOT NULL DEFAULT 'started', "
                "trial_count INTEGER NOT NULL DEFAULT 0, metadata JSONB NOT NULL DEFAULT '{}'::jsonb)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS lifetrade_trials ("
                "client_event_id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES instrument_runs(run_id), "
                "received_at TEXT NOT NULL, task_code TEXT, task_order INTEGER, block_name TEXT, "
                "magnitude TEXT, trial_in_block INTEGER, stimulus_index INTEGER, choice_type TEXT, "
                "response_time_ms DOUBLE PRECISION, payload JSONB NOT NULL)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS experiment_assignments ("
                "assignment_id TEXT PRIMARY KEY, study_code TEXT NOT NULL REFERENCES studies(study_code), "
                "participant_id TEXT NOT NULL REFERENCES participants(participant_id), "
                "condition_code TEXT NOT NULL, invitation_token_hash TEXT UNIQUE NOT NULL, "
                "invitation_token_encrypted TEXT, active INTEGER NOT NULL DEFAULT 1, assigned_at TEXT NOT NULL, "
                "UNIQUE(study_code, participant_id))"
            )
            connection.execute("ALTER TABLE experiment_assignments ADD COLUMN IF NOT EXISTS invitation_token_encrypted TEXT")
            connection.execute("ALTER TABLE experiment_assignments ADD COLUMN IF NOT EXISTS workflow_json TEXT")
            connection.execute("ALTER TABLE experiment_assignments ADD COLUMN IF NOT EXISTS group_link_id TEXT")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS experiment_group_links ("
                "group_link_id TEXT PRIMARY KEY, study_code TEXT NOT NULL REFERENCES studies(study_code), "
                "label TEXT NOT NULL, invitation_token_hash TEXT UNIQUE NOT NULL, "
                "invitation_token_encrypted TEXT NOT NULL, condition_code TEXT NOT NULL, workflow_json TEXT NOT NULL, "
                "active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, deleted_at TEXT)"
            )
            if DATABASE_URL:
                connection.execute("ALTER TABLE experiment_group_links ADD COLUMN IF NOT EXISTS deleted_at TEXT")
            elif "deleted_at" not in {row["name"] for row in connection.execute("PRAGMA table_info(experiment_group_links)").fetchall()}:
                connection.execute("ALTER TABLE experiment_group_links ADD COLUMN deleted_at TEXT")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS experiment_group_claim_requests ("
                "request_id TEXT PRIMARY KEY, group_link_id TEXT NOT NULL REFERENCES experiment_group_links(group_link_id), "
                "request_fingerprint TEXT NOT NULL, assignment_id TEXT REFERENCES experiment_assignments(assignment_id), "
                "created_at TEXT NOT NULL)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_experiment_assignments_group_link "
                "ON experiment_assignments(group_link_id)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS experiment_consents ("
                "assignment_id TEXT NOT NULL REFERENCES experiment_assignments(assignment_id), "
                "consent_version TEXT NOT NULL, accepted_at TEXT NOT NULL, "
                "PRIMARY KEY(assignment_id, consent_version))"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS experiment_stage_start_requests ("
                "request_id TEXT PRIMARY KEY, assignment_id TEXT NOT NULL REFERENCES experiment_assignments(assignment_id), "
                "step_instance_id TEXT, stage_code TEXT NOT NULL, run_id TEXT NOT NULL REFERENCES instrument_runs(run_id), "
                "attempt_number INTEGER NOT NULL, created_at TEXT NOT NULL)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS experiment_stage_progress ("
                "participation_id TEXT NOT NULL REFERENCES study_participations(participation_id), "
                "stage_code TEXT NOT NULL CHECK(stage_code IN ('questionnaire','dd_lifetrade','ultimatum')), "
                "status TEXT NOT NULL DEFAULT 'not_started' "
                "CHECK(status IN ('not_started','in_progress','completed')), "
                "attempt_number INTEGER NOT NULL DEFAULT 0, "
                "current_run_id TEXT REFERENCES instrument_runs(run_id), "
                "draft_payload JSONB NOT NULL DEFAULT '{}'::jsonb, "
                "started_at TEXT, completed_at TEXT, updated_at TEXT NOT NULL, "
                "PRIMARY KEY(participation_id, stage_code))"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS study_workflows ("
                "study_code TEXT PRIMARY KEY, workflow_json TEXT NOT NULL, updated_at TEXT NOT NULL)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS experiment_workflow_progress ("
                "assignment_id TEXT NOT NULL REFERENCES experiment_assignments(assignment_id), "
                "participation_id TEXT NOT NULL REFERENCES study_participations(participation_id), "
                "step_instance_id TEXT NOT NULL, step_order INTEGER NOT NULL, stage_code TEXT NOT NULL, "
                "status TEXT NOT NULL DEFAULT 'not_started' CHECK(status IN ('not_started','in_progress','completed')), "
                "attempt_number INTEGER NOT NULL DEFAULT 0, current_run_id TEXT REFERENCES instrument_runs(run_id), "
                "draft_payload JSONB NOT NULL DEFAULT '{}'::jsonb, started_at TEXT, completed_at TEXT, updated_at TEXT NOT NULL, "
                "PRIMARY KEY(assignment_id,step_instance_id))"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS experiment_time_tracking ("
                "assignment_id TEXT NOT NULL REFERENCES experiment_assignments(assignment_id), "
                "step_instance_id TEXT NOT NULL, step_order INTEGER NOT NULL, stage_code TEXT NOT NULL, "
                "active_seconds DOUBLE PRECISION NOT NULL DEFAULT 0, is_active INTEGER NOT NULL DEFAULT 0, "
                "started_at TEXT NOT NULL, last_seen_at TEXT NOT NULL, completed_at TEXT, "
                "PRIMARY KEY(assignment_id,step_instance_id))"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS experiment_time_event_requests ("
                "event_id TEXT PRIMARY KEY, assignment_id TEXT NOT NULL REFERENCES experiment_assignments(assignment_id), "
                "step_instance_id TEXT NOT NULL, stage_code TEXT NOT NULL, event TEXT NOT NULL, created_at TEXT NOT NULL)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS questionnaire_responses ("
                "run_id TEXT NOT NULL REFERENCES instrument_runs(run_id), "
                "question_code TEXT NOT NULL, response_value TEXT NOT NULL "
                "CHECK(response_value IN ('1','2','3','4','5','NA')), "
                "updated_at TEXT NOT NULL, PRIMARY KEY(run_id, question_code))"
            )
            if DATABASE_URL:
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS ultimatum_trials ("
                    "round_id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES instrument_runs(run_id), "
                    "phase TEXT NOT NULL CHECK(phase IN ('practice','main')), block_code TEXT NOT NULL, "
                    "block_order INTEGER NOT NULL, trial_in_block INTEGER NOT NULL, trial_sequence INTEGER NOT NULL, "
                    "role TEXT NOT NULL CHECK(role IN ('proposer','responder')), "
                    "opponent_condition TEXT NOT NULL CHECK(opponent_condition IN ('agent','coworker')), "
                    "opponent_actual_type TEXT NOT NULL DEFAULT 'computer', "
                    "status TEXT NOT NULL DEFAULT 'prepared' CHECK(status IN ('prepared','completed')), "
                    "participant_share INTEGER CHECK(participant_share BETWEEN 0 AND 10), "
                    "opponent_share INTEGER CHECK(opponent_share BETWEEN 0 AND 10), "
                    "participant_decision TEXT CHECK(participant_decision IN ('accept','reject') OR participant_decision IS NULL), "
                    "opponent_decision TEXT CHECK(opponent_decision IN ('accept','reject') OR opponent_decision IS NULL), "
                    "accepted INTEGER CHECK(accepted IN (0,1) OR accepted IS NULL), "
                    "points_earned INTEGER NOT NULL DEFAULT 0, potential_points_lost INTEGER NOT NULL DEFAULT 0, "
                    "reaction_time_ms DOUBLE PRECISION, wait_duration_ms INTEGER, "
                    "started_at TEXT NOT NULL, decision_screen_shown_at TEXT, responded_at TEXT, "
                    "received_at TEXT NOT NULL, completed_at TEXT, payload JSONB NOT NULL DEFAULT '{}'::jsonb, "
                    "UNIQUE(run_id,phase,block_code,trial_in_block))"
                )
            else:
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS ultimatum_trials ("
                    "round_id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES instrument_runs(run_id), "
                    "phase TEXT NOT NULL CHECK(phase IN ('practice','main')), block_code TEXT NOT NULL, "
                    "block_order INTEGER NOT NULL, trial_in_block INTEGER NOT NULL, trial_sequence INTEGER NOT NULL, "
                    "role TEXT NOT NULL CHECK(role IN ('proposer','responder')), "
                    "opponent_condition TEXT NOT NULL CHECK(opponent_condition IN ('agent','coworker')), "
                    "opponent_actual_type TEXT NOT NULL DEFAULT 'computer', "
                    "status TEXT NOT NULL DEFAULT 'prepared' CHECK(status IN ('prepared','completed')), "
                    "participant_share INTEGER CHECK(participant_share BETWEEN 0 AND 10), "
                    "opponent_share INTEGER CHECK(opponent_share BETWEEN 0 AND 10), "
                    "participant_decision TEXT CHECK(participant_decision IN ('accept','reject') OR participant_decision IS NULL), "
                    "opponent_decision TEXT CHECK(opponent_decision IN ('accept','reject') OR opponent_decision IS NULL), "
                    "accepted INTEGER CHECK(accepted IN (0,1) OR accepted IS NULL), "
                    "points_earned INTEGER NOT NULL DEFAULT 0, potential_points_lost INTEGER NOT NULL DEFAULT 0, "
                    "reaction_time_ms REAL, wait_duration_ms INTEGER, "
                    "started_at TEXT NOT NULL, decision_screen_shown_at TEXT, responded_at TEXT, "
                    "received_at TEXT NOT NULL, completed_at TEXT, payload TEXT NOT NULL DEFAULT '{}', "
                    "UNIQUE(run_id,phase,block_code,trial_in_block))"
                )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_ultimatum_trials_run "
                "ON ultimatum_trials(run_id,phase,block_order,trial_sequence)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_study_participations_participant "
                "ON study_participations(participant_id)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_instrument_runs_participation "
                "ON instrument_runs(participation_id, started_at)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_instrument_runs_version "
                "ON instrument_runs(instrument_version_id, started_at)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_lifetrade_trials_run "
                "ON lifetrade_trials(run_id, received_at)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_lifetrade_trials_task "
                "ON lifetrade_trials(task_code, block_name, magnitude)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_lifetrade_trials_received "
                "ON lifetrade_trials(received_at, client_event_id)"
            )
            _seed_registry(connection)

else:
    DB_PATH = os.getenv("SQLITE_PATH", os.path.join(ROOT, "lifetrade.sqlite3"))
    os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)

    @contextmanager
    def _sqlite_conn() -> Iterator[sqlite3.Connection]:
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

    def conn():
        return observed_connection(_sqlite_conn())

    def init_db() -> None:
        with conn() as connection:
            # Additive schema changes only: legacy sessions/trials remain untouched.
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS studies ("
                "study_code TEXT PRIMARY KEY, study_name TEXT NOT NULL, created_at TEXT NOT NULL)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS participants ("
                "participant_id TEXT PRIMARY KEY, participant_code TEXT UNIQUE NOT NULL, "
                "created_at TEXT NOT NULL)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS study_participations ("
                "participation_id TEXT PRIMARY KEY, study_code TEXT NOT NULL REFERENCES studies(study_code), "
                "participant_id TEXT NOT NULL REFERENCES participants(participant_id), "
                "joined_at TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'active', "
                "UNIQUE(study_code, participant_id))"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS instruments ("
                "instrument_code TEXT PRIMARY KEY, instrument_name TEXT NOT NULL, created_at TEXT NOT NULL)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS instrument_versions ("
                "instrument_version_id TEXT PRIMARY KEY, instrument_code TEXT NOT NULL "
                "REFERENCES instruments(instrument_code), version TEXT NOT NULL, settings TEXT NOT NULL "
                "DEFAULT '{}', created_at TEXT NOT NULL, UNIQUE(instrument_code, version))"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS instrument_runs ("
                "run_id TEXT PRIMARY KEY, participation_id TEXT NOT NULL "
                "REFERENCES study_participations(participation_id), instrument_version_id TEXT NOT NULL "
                "REFERENCES instrument_versions(instrument_version_id), started_at TEXT NOT NULL, "
                "completed_at TEXT, status TEXT NOT NULL DEFAULT 'started', "
                "trial_count INTEGER NOT NULL DEFAULT 0, metadata TEXT NOT NULL DEFAULT '{}')"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS lifetrade_trials ("
                "client_event_id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES instrument_runs(run_id), "
                "received_at TEXT NOT NULL, task_code TEXT, task_order INTEGER, block_name TEXT, "
                "magnitude TEXT, trial_in_block INTEGER, stimulus_index INTEGER, choice_type TEXT, "
                "response_time_ms REAL, payload TEXT NOT NULL)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS experiment_assignments ("
                "assignment_id TEXT PRIMARY KEY, study_code TEXT NOT NULL REFERENCES studies(study_code), "
                "participant_id TEXT NOT NULL REFERENCES participants(participant_id), "
                "condition_code TEXT NOT NULL, invitation_token_hash TEXT UNIQUE NOT NULL, "
                "invitation_token_encrypted TEXT, active INTEGER NOT NULL DEFAULT 1, assigned_at TEXT NOT NULL, "
                "UNIQUE(study_code, participant_id))"
            )
            assignment_columns = {row["name"] for row in connection.execute("PRAGMA table_info(experiment_assignments)").fetchall()}
            if "invitation_token_encrypted" not in assignment_columns:
                connection.execute("ALTER TABLE experiment_assignments ADD COLUMN invitation_token_encrypted TEXT")
            if "workflow_json" not in assignment_columns:
                connection.execute("ALTER TABLE experiment_assignments ADD COLUMN workflow_json TEXT")
            if "group_link_id" not in assignment_columns:
                connection.execute("ALTER TABLE experiment_assignments ADD COLUMN group_link_id TEXT")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS experiment_group_links ("
                "group_link_id TEXT PRIMARY KEY, study_code TEXT NOT NULL REFERENCES studies(study_code), "
                "label TEXT NOT NULL, invitation_token_hash TEXT UNIQUE NOT NULL, "
                "invitation_token_encrypted TEXT NOT NULL, condition_code TEXT NOT NULL, workflow_json TEXT NOT NULL, "
                "active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, deleted_at TEXT)"
            )
            if DATABASE_URL:
                connection.execute("ALTER TABLE experiment_group_links ADD COLUMN IF NOT EXISTS deleted_at TEXT")
            elif "deleted_at" not in {row["name"] for row in connection.execute("PRAGMA table_info(experiment_group_links)").fetchall()}:
                connection.execute("ALTER TABLE experiment_group_links ADD COLUMN deleted_at TEXT")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS experiment_group_claim_requests ("
                "request_id TEXT PRIMARY KEY, group_link_id TEXT NOT NULL REFERENCES experiment_group_links(group_link_id), "
                "request_fingerprint TEXT NOT NULL, assignment_id TEXT REFERENCES experiment_assignments(assignment_id), "
                "created_at TEXT NOT NULL)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_experiment_assignments_group_link "
                "ON experiment_assignments(group_link_id)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS experiment_consents ("
                "assignment_id TEXT NOT NULL REFERENCES experiment_assignments(assignment_id), "
                "consent_version TEXT NOT NULL, accepted_at TEXT NOT NULL, "
                "PRIMARY KEY(assignment_id, consent_version))"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS experiment_stage_start_requests ("
                "request_id TEXT PRIMARY KEY, assignment_id TEXT NOT NULL REFERENCES experiment_assignments(assignment_id), "
                "step_instance_id TEXT, stage_code TEXT NOT NULL, run_id TEXT NOT NULL REFERENCES instrument_runs(run_id), "
                "attempt_number INTEGER NOT NULL, created_at TEXT NOT NULL)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS experiment_stage_progress ("
                "participation_id TEXT NOT NULL REFERENCES study_participations(participation_id), "
                "stage_code TEXT NOT NULL CHECK(stage_code IN ('questionnaire','dd_lifetrade','ultimatum')), "
                "status TEXT NOT NULL DEFAULT 'not_started' "
                "CHECK(status IN ('not_started','in_progress','completed')), "
                "attempt_number INTEGER NOT NULL DEFAULT 0, "
                "current_run_id TEXT REFERENCES instrument_runs(run_id), "
                "draft_payload TEXT NOT NULL DEFAULT '{}', "
                "started_at TEXT, completed_at TEXT, updated_at TEXT NOT NULL, "
                "PRIMARY KEY(participation_id, stage_code))"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS study_workflows ("
                "study_code TEXT PRIMARY KEY, workflow_json TEXT NOT NULL, updated_at TEXT NOT NULL)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS experiment_workflow_progress ("
                "assignment_id TEXT NOT NULL REFERENCES experiment_assignments(assignment_id), "
                "participation_id TEXT NOT NULL REFERENCES study_participations(participation_id), "
                "step_instance_id TEXT NOT NULL, step_order INTEGER NOT NULL, stage_code TEXT NOT NULL, "
                "status TEXT NOT NULL DEFAULT 'not_started' CHECK(status IN ('not_started','in_progress','completed')), "
                "attempt_number INTEGER NOT NULL DEFAULT 0, current_run_id TEXT REFERENCES instrument_runs(run_id), "
                "draft_payload TEXT NOT NULL DEFAULT '{}', started_at TEXT, completed_at TEXT, updated_at TEXT NOT NULL, "
                "PRIMARY KEY(assignment_id,step_instance_id))"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS experiment_time_tracking ("
                "assignment_id TEXT NOT NULL REFERENCES experiment_assignments(assignment_id), "
                "step_instance_id TEXT NOT NULL, step_order INTEGER NOT NULL, stage_code TEXT NOT NULL, "
                "active_seconds REAL NOT NULL DEFAULT 0, is_active INTEGER NOT NULL DEFAULT 0, "
                "started_at TEXT NOT NULL, last_seen_at TEXT NOT NULL, completed_at TEXT, "
                "PRIMARY KEY(assignment_id,step_instance_id))"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS experiment_time_event_requests ("
                "event_id TEXT PRIMARY KEY, assignment_id TEXT NOT NULL REFERENCES experiment_assignments(assignment_id), "
                "step_instance_id TEXT NOT NULL, stage_code TEXT NOT NULL, event TEXT NOT NULL, created_at TEXT NOT NULL)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS questionnaire_responses ("
                "run_id TEXT NOT NULL REFERENCES instrument_runs(run_id), "
                "question_code TEXT NOT NULL, response_value TEXT NOT NULL "
                "CHECK(response_value IN ('1','2','3','4','5','NA')), "
                "updated_at TEXT NOT NULL, PRIMARY KEY(run_id, question_code))"
            )
            if DATABASE_URL:
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS ultimatum_trials ("
                    "round_id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES instrument_runs(run_id), "
                    "phase TEXT NOT NULL CHECK(phase IN ('practice','main')), block_code TEXT NOT NULL, "
                    "block_order INTEGER NOT NULL, trial_in_block INTEGER NOT NULL, trial_sequence INTEGER NOT NULL, "
                    "role TEXT NOT NULL CHECK(role IN ('proposer','responder')), "
                    "opponent_condition TEXT NOT NULL CHECK(opponent_condition IN ('agent','coworker')), "
                    "opponent_actual_type TEXT NOT NULL DEFAULT 'computer', "
                    "status TEXT NOT NULL DEFAULT 'prepared' CHECK(status IN ('prepared','completed')), "
                    "participant_share INTEGER CHECK(participant_share BETWEEN 0 AND 10), "
                    "opponent_share INTEGER CHECK(opponent_share BETWEEN 0 AND 10), "
                    "participant_decision TEXT CHECK(participant_decision IN ('accept','reject') OR participant_decision IS NULL), "
                    "opponent_decision TEXT CHECK(opponent_decision IN ('accept','reject') OR opponent_decision IS NULL), "
                    "accepted INTEGER CHECK(accepted IN (0,1) OR accepted IS NULL), "
                    "points_earned INTEGER NOT NULL DEFAULT 0, potential_points_lost INTEGER NOT NULL DEFAULT 0, "
                    "reaction_time_ms DOUBLE PRECISION, wait_duration_ms INTEGER, "
                    "started_at TEXT NOT NULL, decision_screen_shown_at TEXT, responded_at TEXT, "
                    "received_at TEXT NOT NULL, completed_at TEXT, payload JSONB NOT NULL DEFAULT '{}'::jsonb, "
                    "UNIQUE(run_id,phase,block_code,trial_in_block))"
                )
            else:
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS ultimatum_trials ("
                    "round_id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES instrument_runs(run_id), "
                    "phase TEXT NOT NULL CHECK(phase IN ('practice','main')), block_code TEXT NOT NULL, "
                    "block_order INTEGER NOT NULL, trial_in_block INTEGER NOT NULL, trial_sequence INTEGER NOT NULL, "
                    "role TEXT NOT NULL CHECK(role IN ('proposer','responder')), "
                    "opponent_condition TEXT NOT NULL CHECK(opponent_condition IN ('agent','coworker')), "
                    "opponent_actual_type TEXT NOT NULL DEFAULT 'computer', "
                    "status TEXT NOT NULL DEFAULT 'prepared' CHECK(status IN ('prepared','completed')), "
                    "participant_share INTEGER CHECK(participant_share BETWEEN 0 AND 10), "
                    "opponent_share INTEGER CHECK(opponent_share BETWEEN 0 AND 10), "
                    "participant_decision TEXT CHECK(participant_decision IN ('accept','reject') OR participant_decision IS NULL), "
                    "opponent_decision TEXT CHECK(opponent_decision IN ('accept','reject') OR opponent_decision IS NULL), "
                    "accepted INTEGER CHECK(accepted IN (0,1) OR accepted IS NULL), "
                    "points_earned INTEGER NOT NULL DEFAULT 0, potential_points_lost INTEGER NOT NULL DEFAULT 0, "
                    "reaction_time_ms REAL, wait_duration_ms INTEGER, "
                    "started_at TEXT NOT NULL, decision_screen_shown_at TEXT, responded_at TEXT, "
                    "received_at TEXT NOT NULL, completed_at TEXT, payload TEXT NOT NULL DEFAULT '{}', "
                    "UNIQUE(run_id,phase,block_code,trial_in_block))"
                )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_ultimatum_trials_run "
                "ON ultimatum_trials(run_id,phase,block_order,trial_sequence)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_study_participations_participant "
                "ON study_participations(participant_id)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_instrument_runs_participation "
                "ON instrument_runs(participation_id, started_at)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_instrument_runs_version "
                "ON instrument_runs(instrument_version_id, started_at)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_lifetrade_trials_run "
                "ON lifetrade_trials(run_id, received_at)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_lifetrade_trials_task "
                "ON lifetrade_trials(task_code, block_name, magnitude)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_lifetrade_trials_received "
                "ON lifetrade_trials(received_at, client_event_id)"
            )
            _seed_registry(connection)


def _seed_registry(connection: Any) -> None:
    """Idempotently register the study and every canonical module."""
    initialize_client_timing(connection)
    instrument_rows = list({code: name for code, _version, name in STAGE_TO_INSTRUMENT.values()}.items())
    for instrument_code, instrument_name in instrument_rows:
        if DATABASE_URL:
            connection.execute(
                "INSERT INTO instruments(instrument_code,instrument_name,created_at) VALUES(%s,%s,%s) "
                "ON CONFLICT(instrument_code) DO NOTHING",
                (instrument_code, instrument_name, now()),
            )
        else:
            connection.execute(
                "INSERT OR IGNORE INTO instruments(instrument_code,instrument_name,created_at) VALUES(?,?,?)",
                (instrument_code, instrument_name, now()),
            )
    # These version rows are fixed by the deployed protocol. Seed them at process
    # initialization so participant stage-start requests need only one indexed SELECT.
    for stage_code, (instrument_code, version, _instrument_name) in STAGE_TO_INSTRUMENT.items():
        settings = {"module": stage_code, "schema_version": 1}
        if DATABASE_URL:
            connection.execute(
                "INSERT INTO instrument_versions(instrument_version_id,instrument_code,version,settings,created_at) "
                "VALUES(%s,%s,%s,%s,%s) ON CONFLICT(instrument_code,version) DO NOTHING",
                (str(uuid.uuid4()), instrument_code, version, Jsonb(settings), now()),
            )
        else:
            connection.execute(
                "INSERT OR IGNORE INTO instrument_versions(instrument_version_id,instrument_code,version,settings,created_at) "
                "VALUES(?,?,?,?,?)",
                (str(uuid.uuid4()), instrument_code, version, json.dumps(settings, ensure_ascii=False), now()),
            )

    if DATABASE_URL:
        connection.execute(
            "INSERT INTO studies(study_code,study_name,created_at) VALUES(%s,%s,%s) "
            "ON CONFLICT(study_code) DO NOTHING",
            (STUDY_CODE, STUDY_NAME, now()),
        )
    else:
        connection.execute(
            "INSERT OR IGNORE INTO studies(study_code,study_name,created_at) VALUES(?,?,?)",
            (STUDY_CODE, STUDY_NAME, now()),
        )


init_db()

EXPORT_IDLE_SECONDS = max(0, int(os.getenv("BARSAN_EXPORT_IDLE_SECONDS", "0")))


def _recent_task_activity() -> bool:
    if not EXPORT_IDLE_SECONDS:
        return False
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=EXPORT_IDLE_SECONDS)).isoformat()
    marker = "%s" if DATABASE_URL else "?"
    with conn() as connection:
        row = connection.execute(
            "SELECT 1 AS busy WHERE EXISTS (SELECT 1 FROM experiment_client_clocks WHERE received_at>" + marker + ") "
            "OR EXISTS (SELECT 1 FROM experiment_time_tracking WHERE stage_code IN ('dd_lifetrade','ultimatum') AND last_seen_at>" + marker + ") "
            "OR EXISTS (SELECT 1 FROM instrument_runs r JOIN instrument_versions v USING(instrument_version_id) "
            "WHERE v.instrument_code IN ('lifetrade','ultimatum') AND r.status='in_progress' AND r.started_at>" + marker + ")",
            (cutoff, cutoff, cutoff)).fetchone()
    return row is not None


class SessionIn(BaseModel):
    participant_code: str = Field(min_length=1, max_length=100)
    experiment_version: str = Field(default="online-mvp-0.1", min_length=1, max_length=100)
    user_agent: str | None = Field(default=None, max_length=1000)
    screen_width: int | None = Field(default=None, ge=1, le=20000)
    screen_height: int | None = Field(default=None, ge=1, le=20000)


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
    trial_count: int = Field(default=0, ge=0)

StageCode = Literal["questionnaire", "dd_lifetrade", "ultimatum", "intermission"]
WorkflowModuleCode = Literal[
    "questionnaire", "dd_lifetrade", "ultimatum", "ultimatum_frame", "ultimatum_control",
    "dd_standalone", "intermission"
]


class AssignmentItemIn(BaseModel):
    participant_code: str = Field(min_length=1, max_length=100)
    condition_code: str | None = Field(default=None, max_length=64)


class WorkflowStepIn(BaseModel):
    step_id: str = Field(min_length=1, max_length=80)
    module_code: WorkflowModuleCode


class WorkflowConfigIn(BaseModel):
    steps: list[WorkflowStepIn] = Field(min_length=1)


class AssignmentBatchIn(BaseModel):
    assignments: list[AssignmentItemIn] = Field(min_length=1, max_length=500)
    steps: list[WorkflowStepIn] | None = Field(default=None, min_length=1)


class GroupLinkCreateIn(BaseModel):
    scope: Literal["main", "test"] = "main"
    label: str = Field(default="لینک گروهی", max_length=100)
    steps: list[WorkflowStepIn] | None = Field(default=None, min_length=1)


class GroupLinkStatusBatchIn(BaseModel):
    scope: Literal["main", "test"] = "main"
    group_link_ids: list[str] = Field(min_length=1, max_length=5000)
    active: bool
    include_assignments: bool = False


class GroupLinkDeleteIn(BaseModel):
    scope: Literal["main", "test"]
    group_link_ids: list[str] = Field(min_length=1, max_length=5000)
    confirm_phrase: str = Field(min_length=1, max_length=40)


class GroupClaimIn(BaseModel):
    group_token: str = Field(min_length=20, max_length=200)
    participant_code: str | None = Field(default=None, max_length=100)
    assign_random: bool = False
    # Stable UUID lets the server return the same allocation after a lost response.
    claim_request_id: str | None = Field(default=None, min_length=16, max_length=100)


class AssignmentStatusBatchIn(BaseModel):
    scope: Literal["main", "test"] = "main"
    assignment_ids: list[str] = Field(min_length=1, max_length=5000)
    active: bool


class AssignmentIdBatchIn(BaseModel):
    scope: Literal["main", "test"] = "main"
    assignment_ids: list[str] = Field(min_length=1, max_length=5000)


class AssignmentReissueIn(BaseModel):
    scope: Literal["main", "test"] = "main"
    assignment_id: str = Field(min_length=1, max_length=100)


class InactiveAssignmentDeleteIn(BaseModel):
    scope: Literal["main", "test"]
    confirm_phrase: str = Field(min_length=1, max_length=40)
    assignment_ids: list[str] | None = Field(default=None, min_length=1, max_length=5000)


class ModuleTestLinkIn(BaseModel):
    stage_code: Literal["questionnaire", "dd_lifetrade", "ultimatum"]
    condition_code: Literal["A", "B"] = "A"


class MaintenanceResetIn(BaseModel):
    scope: Literal["main", "test"]
    confirm_phrase: str = Field(min_length=1, max_length=40)


class ExperimentIdentityIn(BaseModel):
    invitation_token: str = Field(min_length=20, max_length=200)
    participant_code: str = Field(min_length=1, max_length=100)


class ExperimentConsentIn(ExperimentIdentityIn):
    accepted: bool


class StageStartIn(ExperimentIdentityIn):
    stage_code: StageCode
    step_id: str | None = Field(default=None, min_length=1, max_length=80)
    # Stable only within one in-memory start operation; a page reload creates a new ID.
    start_request_id: str | None = Field(default=None, min_length=1, max_length=100)
    # Backward-compatible recovery path for older clients.
    resume_if_active: bool = False


class StageDraftIn(ExperimentIdentityIn):
    stage_code: Literal["questionnaire"] = "questionnaire"
    run_id: str = Field(min_length=1, max_length=100)
    step_id: str | None = Field(default=None, min_length=1, max_length=80)
    draft_payload: dict[str, Any]


class QuestionnaireAnswerIn(ExperimentIdentityIn):
    run_id: str = Field(min_length=1, max_length=100)
    step_id: str | None = Field(default=None, min_length=1, max_length=80)
    question_code: str = Field(min_length=1, max_length=16)
    response_value: Literal["1", "2", "3", "4", "5", "NA"]
    current_question_index: int = Field(default=0, ge=0, le=100)

class QuestionnaireAnswerBatchItem(BaseModel):
    question_code: str = Field(min_length=1, max_length=16)
    response_value: Literal["1", "2", "3", "4", "5", "NA"]
    current_question_index: int = Field(ge=0, le=100)


class QuestionnaireAnswersBatchIn(ExperimentIdentityIn):
    run_id: str = Field(min_length=1, max_length=100)
    step_id: str | None = Field(default=None, min_length=1, max_length=80)
    answers: list[QuestionnaireAnswerBatchItem] = Field(min_length=1, max_length=10)


class UltimatumEventItem(BaseModel):
    round_id: str = Field(min_length=1, max_length=100)
    event_timestamps: dict[str, str] = Field(min_length=1, max_length=10)
    wait_duration_ms: int | None = Field(default=None, ge=0, le=60000)


class ClientTimingIn(ExperimentIdentityIn):
    checkpoints: list[TimingCheckpoint] = Field(min_length=1, max_length=25)


class StageCompleteIn(ExperimentIdentityIn):
    stage_code: StageCode
    run_id: str = Field(min_length=1, max_length=100)
    step_id: str | None = Field(default=None, min_length=1, max_length=80)
    final_payload: dict[str, Any] | None = None
    auxiliary_events: list[UltimatumEventItem] = Field(default_factory=list, max_length=25)
    timing_checkpoints: list[TimingCheckpoint] = Field(default_factory=list, max_length=25)


class ExperimentTimeIn(ExperimentIdentityIn):
    step_id: str = Field(min_length=1, max_length=80)
    stage_code: StageCode
    event: Literal["start", "heartbeat", "pause", "complete"]
    event_id: str | None = Field(default=None, min_length=1, max_length=100)


class UltimatumPrepareIn(ExperimentIdentityIn):
    run_id: str = Field(min_length=1, max_length=100)
    phase: Literal["practice", "main"]
    block_code: Literal[
        "practice_proposer_agent", "practice_responder_coworker",
        "proposer_agent", "responder_agent", "proposer_coworker", "responder_coworker"
    ]
    block_order: int = Field(ge=0, le=4)
    trial_in_block: int = Field(ge=1, le=5)
    trial_sequence: int = Field(ge=0, le=20)
    client_metadata: dict[str, Any] = Field(default_factory=dict)


class UltimatumResolveIn(ExperimentIdentityIn):
    run_id: str = Field(min_length=1, max_length=100)
    round_id: str = Field(min_length=1, max_length=100)
    participant_offer_to_opponent: int | None = Field(default=None, ge=0, le=10)
    participant_decision: Literal["accept", "reject"] | None = None
    decision_screen_shown_at: str | None = Field(default=None, max_length=80)
    responded_at: str | None = Field(default=None, max_length=80)
    reaction_time_ms: float | None = Field(default=None, ge=0, le=3600000)
    wait_duration_ms: int | None = Field(default=None, ge=0, le=60000)
    planned_wait_duration_ms: int | None = Field(default=None, ge=0, le=60000)
    prepare_next: bool = False
    auxiliary_events: list[UltimatumEventItem] = Field(default_factory=list, max_length=25)
    client_metadata: dict[str, Any] = Field(default_factory=dict)
    client_event_timestamps: dict[str, str] = Field(default_factory=dict)


class UltimatumEventsIn(ExperimentIdentityIn):
    run_id: str = Field(min_length=1, max_length=100)
    round_id: str = Field(min_length=1, max_length=100)
    event_timestamps: dict[str, str] = Field(min_length=1, max_length=10)


class UltimatumEventsBatchIn(ExperimentIdentityIn):
    run_id: str = Field(min_length=1, max_length=100)
    events: list[UltimatumEventItem] = Field(min_length=1, max_length=25)


def _ensure_participant_and_run(data: SessionIn) -> tuple[str, str]:
    participant_code = normalize_participant_code(data.participant_code)
    participant_id = str(uuid.uuid4())
    participation_id = str(uuid.uuid4())
    run_id = str(uuid.uuid4())
    version = data.experiment_version.strip() or "online-mvp-0.1"
    metadata = {
        "user_agent": data.user_agent,
        "screen_width": data.screen_width,
        "screen_height": data.screen_height,
    }
    settings = {"schema_version": 1, "capture": "LifeTrade online experiment"}

    with conn() as connection:
        if not DATABASE_URL:
            connection.execute("BEGIN IMMEDIATE")

        if DATABASE_URL:
            connection.execute(
                "INSERT INTO participants(participant_id,participant_code,created_at) "
                "VALUES(%s,%s,%s) ON CONFLICT(participant_code) DO NOTHING",
                (participant_id, participant_code, now()),
            )
            participant = connection.execute(
                "SELECT participant_id FROM participants WHERE participant_code=%s",
                (participant_code,),
            ).fetchone()
            connection.execute(
                "INSERT INTO study_participations(participation_id,study_code,participant_id,joined_at) "
                "VALUES(%s,%s,%s,%s) ON CONFLICT(study_code,participant_id) DO NOTHING",
                (participation_id, STUDY_CODE, participant["participant_id"], now()),
            )
            participation = connection.execute(
                "SELECT participation_id FROM study_participations "
                "WHERE study_code=%s AND participant_id=%s",
                (STUDY_CODE, participant["participant_id"]),
            ).fetchone()
            connection.execute(
                "INSERT INTO instrument_versions(instrument_version_id,instrument_code,version,settings,created_at) "
                "VALUES(%s,%s,%s,%s,%s) ON CONFLICT(instrument_code,version) DO NOTHING",
                (str(uuid.uuid4()), INSTRUMENT_CODE, version, Jsonb(settings), now()),
            )
            instrument_version = connection.execute(
                "SELECT instrument_version_id FROM instrument_versions "
                "WHERE instrument_code=%s AND version=%s",
                (INSTRUMENT_CODE, version),
            ).fetchone()
            connection.execute(
                "INSERT INTO instrument_runs(run_id,participation_id,instrument_version_id,started_at,metadata) "
                "VALUES(%s,%s,%s,%s,%s)",
                (
                    run_id,
                    participation["participation_id"],
                    instrument_version["instrument_version_id"],
                    now(),
                    Jsonb(metadata),
                ),
            )
        else:
            connection.execute(
                "INSERT OR IGNORE INTO participants(participant_id,participant_code,created_at) VALUES(?,?,?)",
                (participant_id, participant_code, now()),
            )
            participant = connection.execute(
                "SELECT participant_id FROM participants WHERE participant_code=?",
                (participant_code,),
            ).fetchone()
            connection.execute(
                "INSERT OR IGNORE INTO study_participations(participation_id,study_code,participant_id,joined_at) "
                "VALUES(?,?,?,?)",
                (participation_id, STUDY_CODE, participant["participant_id"], now()),
            )
            participation = connection.execute(
                "SELECT participation_id FROM study_participations WHERE study_code=? AND participant_id=?",
                (STUDY_CODE, participant["participant_id"]),
            ).fetchone()
            connection.execute(
                "INSERT OR IGNORE INTO instrument_versions(instrument_version_id,instrument_code,version,settings,created_at) "
                "VALUES(?,?,?,?,?)",
                (
                    str(uuid.uuid4()),
                    INSTRUMENT_CODE,
                    version,
                    json.dumps(settings, ensure_ascii=False, separators=(",", ":")),
                    now(),
                ),
            )
            instrument_version = connection.execute(
                "SELECT instrument_version_id FROM instrument_versions WHERE instrument_code=? AND version=?",
                (INSTRUMENT_CODE, version),
            ).fetchone()
            connection.execute(
                "INSERT INTO instrument_runs(run_id,participation_id,instrument_version_id,started_at,metadata) "
                "VALUES(?,?,?,?,?)",
                (
                    run_id,
                    participation["participation_id"],
                    instrument_version["instrument_version_id"],
                    now(),
                    json.dumps(metadata, ensure_ascii=False, separators=(",", ":")),
                ),
            )

    return run_id, participant_code


@app.get("/api/health")
def health():
    database_type = "postgresql" if DATABASE_URL else "sqlite"
    try:
        with conn() as connection:
            connection.execute("SELECT 1").fetchone()
        return {
            "ok": True,
            "service": "barsan-experiment",
            "database": database_type,
            "database_connected": True,
            "study_code": STUDY_CODE,
            "instrument_code": INSTRUMENT_CODE,
            "time": now(),
        }
    except HTTPException:
        raise
    except Exception as exc:
        print(f"ERROR health check: {type(exc).__name__}")
        raise HTTPException(
            status_code=503,
            detail={
                "ok": False,
                "service": "barsan-experiment",
                "database": database_type,
                "database_connected": False,
                "time": now(),
            },
        ) from exc


@app.post("/api/session")
def create_session(data: SessionIn):
    try:
        run_id, participant_code = _ensure_participant_and_run(data)
    except HTTPException:
        raise
    except Exception as exc:
        print(f"ERROR creating LifeTrade run: {type(exc).__name__}")
        raise HTTPException(500, "Could not start LifeTrade session") from exc
    # Keep session_id for the existing browser client; it now points to instrument_runs.run_id.
    return {
        "session_id": run_id,
        "run_id": run_id,
        "participant_code": participant_code,
        "instrument_code": INSTRUMENT_CODE,
    }



def _json_value(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except (TypeError, json.JSONDecodeError):
            return {}
    return dict(value) if isinstance(value, dict) else {}


def _json_param(value: dict[str, Any]) -> Any:
    return Jsonb(value) if DATABASE_URL else json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _ensure_participation(
    connection: Any,
    participant_code: str,
    study_code: str = STUDY_CODE,
    study_name: str | None = None,
) -> tuple[str, str]:
    """Create/reuse a participant in a named study without generating their code."""
    participant_id = str(uuid.uuid4())
    participation_id = str(uuid.uuid4())
    resolved_study_name = study_name or study_code
    timestamp = now()
    if DATABASE_URL:
        connection.execute(
            "INSERT INTO studies(study_code,study_name,created_at) VALUES(%s,%s,%s) "
            "ON CONFLICT(study_code) DO NOTHING",
            (study_code, resolved_study_name, timestamp),
        )
        connection.execute(
            "INSERT INTO participants(participant_id,participant_code,created_at) VALUES(%s,%s,%s) "
            "ON CONFLICT(participant_code) DO NOTHING",
            (participant_id, participant_code, timestamp),
        )
        participant = connection.execute(
            "SELECT participant_id FROM participants WHERE participant_code=%s",
            (participant_code,),
        ).fetchone()
        connection.execute(
            "INSERT INTO study_participations(participation_id,study_code,participant_id,joined_at) "
            "VALUES(%s,%s,%s,%s) ON CONFLICT(study_code,participant_id) DO NOTHING",
            (participation_id, study_code, participant["participant_id"], timestamp),
        )
        participation = connection.execute(
            "SELECT participation_id FROM study_participations WHERE study_code=%s AND participant_id=%s",
            (study_code, participant["participant_id"]),
        ).fetchone()
    else:
        connection.execute(
            "INSERT OR IGNORE INTO studies(study_code,study_name,created_at) VALUES(?,?,?)",
            (study_code, resolved_study_name, timestamp),
        )
        connection.execute(
            "INSERT OR IGNORE INTO participants(participant_id,participant_code,created_at) VALUES(?,?,?)",
            (participant_id, participant_code, timestamp),
        )
        participant = connection.execute(
            "SELECT participant_id FROM participants WHERE participant_code=?",
            (participant_code,),
        ).fetchone()
        connection.execute(
            "INSERT OR IGNORE INTO study_participations(participation_id,study_code,participant_id,joined_at) "
            "VALUES(?,?,?,?)",
            (participation_id, study_code, participant["participant_id"], timestamp),
        )
        participation = connection.execute(
            "SELECT participation_id FROM study_participations WHERE study_code=? AND participant_id=?",
            (study_code, participant["participant_id"]),
        ).fetchone()
    return participant["participant_id"], participation["participation_id"]


def _test_module_from_study_code(study_code: str) -> str | None:
    prefix = TEST_STUDY_PREFIX + "module-"
    if not study_code.startswith(prefix):
        return None
    suffix = study_code[len(prefix):]
    for stage_code in STAGE_ORDER:
        if suffix.endswith("-" + stage_code):
            return stage_code
    return None


def _seed_completed_test_stage(connection: Any, participation_id: str, stage_code: str) -> None:
    """Insert an auditable synthetic completed prerequisite for a module-only test link."""
    instrument_code, version, instrument_name = STAGE_TO_INSTRUMENT[stage_code]
    timestamp = now()
    marker = "%s" if DATABASE_URL else "?"
    settings = {"module": stage_code, "test_prerequisite": True, "schema_version": 1}
    if DATABASE_URL:
        connection.execute(
            "INSERT INTO instruments(instrument_code,instrument_name,created_at) VALUES(%s,%s,%s) "
            "ON CONFLICT(instrument_code) DO NOTHING",
            (instrument_code, instrument_name, timestamp),
        )
        connection.execute(
            "INSERT INTO instrument_versions(instrument_version_id,instrument_code,version,settings,created_at) "
            "VALUES(%s,%s,%s,%s,%s) ON CONFLICT(instrument_code,version) DO NOTHING",
            (str(uuid.uuid4()), instrument_code, version, _json_param(settings), timestamp),
        )
    else:
        connection.execute(
            "INSERT OR IGNORE INTO instruments(instrument_code,instrument_name,created_at) VALUES(?,?,?)",
            (instrument_code, instrument_name, timestamp),
        )
        connection.execute(
            "INSERT OR IGNORE INTO instrument_versions(instrument_version_id,instrument_code,version,settings,created_at) "
            "VALUES(?,?,?,?,?)",
            (str(uuid.uuid4()), instrument_code, version, _json_param(settings), timestamp),
        )
    version_row = connection.execute(
        "SELECT instrument_version_id FROM instrument_versions WHERE instrument_code=" + marker
        + " AND version=" + marker,
        (instrument_code, version),
    ).fetchone()
    run_id = str(uuid.uuid4())
    metadata = _json_param({"attempt_number": 1, "test_prerequisite": True, "module": stage_code})
    connection.execute(
        "INSERT INTO instrument_runs(run_id,participation_id,instrument_version_id,started_at,completed_at,"
        "status,trial_count,metadata) VALUES(" + ",".join([marker] * 8) + ")",
        (run_id, participation_id, version_row["instrument_version_id"], timestamp, timestamp,
         "completed", 0, metadata),
    )
    empty_payload = _json_param({"test_prerequisite": True, "module": stage_code})
    connection.execute(
        "INSERT INTO experiment_stage_progress(participation_id,stage_code,status,attempt_number,current_run_id,"
        "draft_payload,started_at,completed_at,updated_at) VALUES("
        + ",".join([marker] * 9)
        + ") ON CONFLICT(participation_id,stage_code) DO UPDATE SET "
        "status='completed',attempt_number=1,current_run_id=excluded.current_run_id,"
        "draft_payload=excluded.draft_payload,started_at=excluded.started_at,"
        "completed_at=excluded.completed_at,updated_at=excluded.updated_at",
        (participation_id, stage_code, "completed", 1, run_id, empty_payload, timestamp, timestamp, timestamp),
    )


def _scope_filter(scope: str, column: str, marker: str) -> tuple[str, tuple[Any, ...]]:
    if scope == "test":
        return "substr(" + column + ",1,12)='barsan-test-'", ()
    return column + "=" + marker, (STUDY_CODE,)


def _scope_rows(connection: Any, scope: str, table: str, column: str = "study_code") -> list[dict[str, Any]]:
    marker = "%s" if DATABASE_URL else "?"
    condition, params = _scope_filter(scope, column, marker)
    return [dict(row) for row in connection.execute(
        "SELECT * FROM " + table + " WHERE " + condition,
        params,
    ).fetchall()]


def _default_workflow_steps() -> list[dict[str, str]]:
    return [dict(step) for step in DEFAULT_WORKFLOW]


def _normalize_workflow_steps(raw_steps: Any) -> list[dict[str, str]]:
    if not isinstance(raw_steps, (list, tuple)) or not raw_steps:
        raise HTTPException(422, "روند آزمایش باید حداقل یک و حداکثر ۶۰ ماژول داشته باشد.")
    normalized = []
    seen_ids = set()
    for raw in raw_steps:
        item = raw.model_dump() if hasattr(raw, "model_dump") else raw
        if not isinstance(item, dict):
            raise HTTPException(422, "ساختار یکی از ماژول‌های روند آزمایش معتبر نیست.")
        step_id = str(item.get("step_id") or "").strip()
        module_code = str(item.get("module_code") or "").strip()
        if (
            not step_id or len(step_id) > 80
            or any(not (character.isalnum() or character in "-_") for character in step_id)
            or module_code not in WORKFLOW_MODULE_CODES
            or step_id in seen_ids
        ):
            raise HTTPException(422, "شناسه یا نوع یکی از ماژول‌های روند آزمایش معتبر نیست.")
        seen_ids.add(step_id)
        normalized.append({"step_id": step_id, "module_code": module_code})
    return normalized


def _canonical_workflow_stage(module_code: str) -> str:
    if module_code == "dd_standalone":
        return "dd_lifetrade"
    return "ultimatum" if module_code in ULTIMATUM_WORKFLOW_VARIANTS else module_code


def _condition_group(condition_code: Any) -> str:
    """Normalize optional legacy A/B values; new assignments derive this from the workflow."""
    value = str(condition_code or "").strip().upper()
    if value in {"A", "FRAME", "FRAMING"}:
        return "A"
    if value in {"B", "CONTROL"}:
        return "B"
    raise HTTPException(422, "مقدار condition_code معتبر نیست؛ در روند جدید این فیلد باید حذف شود و نوع ماژول از تایم‌لاین تعیین شود.")


def _workflow_condition_code(steps: list[dict[str, str]]) -> str:
    variants = {step["module_code"] for step in steps} & set(ULTIMATUM_WORKFLOW_VARIANTS)
    if variants == {"ultimatum_frame"}:
        return "A"
    if variants == {"ultimatum_control"}:
        return "B"
    if len(variants) == 2:
        return "MIXED"
    return "NONE"


def _workflow_ultimatum_variant(steps: list[dict[str, str]]) -> str | None:
    variants = []
    for step in steps:
        label = ULTIMATUM_WORKFLOW_VARIANTS.get(step["module_code"])
        if label and label not in variants:
            variants.append(label)
    return " + ".join(variants) if variants else None


def _workflow_module_label(module_code: str) -> str:
    return {
        "questionnaire": "پرسشنامه",
        "dd_lifetrade": "DD / Life-Trade",
        "dd_standalone": "DD مستقل · ۵۰ / ۱۰۰ میلیون · ۵ دور",
        "intermission": "استراحت بین بازی‌ها",
        "ultimatum": "بازی Ultimatum",
        "ultimatum_frame": "Ultimatum — Frame",
        "ultimatum_control": "Ultimatum — Control",
    }.get(module_code, module_code)


def _load_study_workflow(connection: Any, study_code: str) -> list[dict[str, str]]:
    marker = "%s" if DATABASE_URL else "?"
    row = connection.execute(
        "SELECT workflow_json FROM study_workflows WHERE study_code=" + marker,
        (study_code,),
    ).fetchone()
    if not row:
        return _default_workflow_steps()
    try:
        normalized = _normalize_workflow_steps(json.loads(row["workflow_json"]))
        previous_default = [
            {"step_id": "default-questionnaire", "module_code": "questionnaire"},
            {"step_id": "default-dd-lifetrade", "module_code": "dd_lifetrade"},
            {"step_id": "default-ultimatum-frame", "module_code": "ultimatum_frame"},
            {"step_id": "default-ultimatum-control", "module_code": "ultimatum_control"},
        ]
        # The former built-in pair is not a user-selected experimental design.
        # Replace only that exact default snapshot; preserve custom workflows.
        if normalized == previous_default:
            return _default_workflow_steps()
        return normalized
    except (TypeError, ValueError, KeyError, HTTPException):
        return _default_workflow_steps()


def _workflow_for_assignment(assignment: dict[str, Any]) -> list[dict[str, str]]:
    module_only = _test_module_from_study_code(str(assignment.get("study_code") or ""))
    if module_only:
        return [{"step_id": "module-test-" + module_only, "module_code": module_only}]
    raw = assignment.get("workflow_json")
    if raw:
        try:
            if isinstance(raw, str):
                raw = json.loads(raw)
            return _normalize_workflow_steps(raw)
        except (TypeError, ValueError, HTTPException):
            pass
    return _default_workflow_steps()


def _workflow_progress_rows(connection: Any, assignment_id: str) -> dict[str, dict[str, Any]]:
    marker = "%s" if DATABASE_URL else "?"
    rows = connection.execute(
        "SELECT step_instance_id,step_order,stage_code,status,attempt_number,current_run_id,"
        "draft_payload,started_at,completed_at,updated_at "
        "FROM experiment_workflow_progress WHERE assignment_id=" + marker,
        (assignment_id,),
    ).fetchall()
    return {row["step_instance_id"]: dict(row) for row in rows}


def _lookup_assignment(
    connection: Any,
    invitation_token: str,
    participant_code: str,
    *,
    require_consent: bool = False,
    lock_for_start: bool = False,
) -> dict[str, Any]:
    """Resolve one active invitation; optionally validate consent in the same DB round-trip."""
    token_hash = hashlib.sha256(invitation_token.encode("utf-8")).hexdigest()
    marker = "%s" if DATABASE_URL else "?"
    consent_select = (
        ", EXISTS(SELECT 1 FROM experiment_consents ec "
        "WHERE ec.assignment_id=a.assignment_id AND ec.consent_version=" + marker
        + ") AS consent_accepted"
        if require_consent else ""
    )
    query = (
        "SELECT a.assignment_id,a.study_code,a.participant_id,a.condition_code,a.workflow_json,"
        "sp.participation_id,p.participant_code"
        + consent_select
        + " FROM experiment_assignments a "
        "JOIN participants p ON p.participant_id=a.participant_id "
        "JOIN study_participations sp ON sp.participant_id=a.participant_id "
        "AND sp.study_code=a.study_code "
        "WHERE p.participant_code=" + marker
        + " AND a.invitation_token_hash=" + marker + " AND a.active=1"
    )
    normalized_code = normalize_participant_code(participant_code)
    params = (
        (CONSENT_VERSION, normalized_code, token_hash)
        if require_consent else (normalized_code, token_hash)
    )
    if lock_for_start and DATABASE_URL:
        query += " FOR UPDATE OF a"
    row = connection.execute(query, params).fetchone()
    if not row:
        # A generic error avoids distinguishing a bad code from a bad invitation link.
        raise HTTPException(403, "کد شرکت‌کننده یا لینک اختصاصی معتبر نیست.")
    if require_consent and not bool(row["consent_accepted"]):
        raise HTTPException(403, "رضایت برای مشارکت ثبت نشده است.")
    return dict(row)

def _require_consent(connection: Any, assignment_id: str) -> None:
    marker = "%s" if DATABASE_URL else "?"
    row = connection.execute(
        "SELECT 1 FROM experiment_consents WHERE assignment_id=" + marker
        + " AND consent_version=" + marker,
        (assignment_id, CONSENT_VERSION),
    ).fetchone()
    if not row:
        raise HTTPException(403, "رضایت برای مشارکت ثبت نشده است.")


def _get_stage_rows(connection: Any, participation_id: str) -> dict[str, dict[str, Any]]:
    marker = "%s" if DATABASE_URL else "?"
    rows = connection.execute(
        "SELECT stage_code,status,attempt_number,current_run_id,draft_payload,started_at,completed_at,updated_at "
        "FROM experiment_stage_progress WHERE participation_id=" + marker,
        (participation_id,),
    ).fetchall()
    return {row["stage_code"]: dict(row) for row in rows}


def _lookup_questionnaire_assignment_state(
    connection: Any,
    invitation_token: str,
    participant_code: str,
    step_id: str | None = None,
) -> dict[str, Any]:
    """Resolve invitation, consent and questionnaire progress in one indexed read."""
    token_hash = hashlib.sha256(invitation_token.encode("utf-8")).hexdigest()
    marker = "%s" if DATABASE_URL else "?"
    query = (
        "SELECT a.assignment_id,a.study_code,a.participant_id,a.condition_code,a.workflow_json,"
        "sp.participation_id,p.participant_code,"
        "EXISTS(SELECT 1 FROM experiment_consents ec "
        "WHERE ec.assignment_id=a.assignment_id AND ec.consent_version=" + marker
        + ") AS consent_accepted,"
        "progress.status AS questionnaire_status,"
        "progress.current_run_id AS questionnaire_run_id,"
        "progress.draft_payload AS questionnaire_draft_payload,"
        "workflow.status AS workflow_status,"
        "workflow.current_run_id AS workflow_run_id "
        "FROM experiment_assignments a "
        "JOIN participants p ON p.participant_id=a.participant_id "
        "JOIN study_participations sp ON sp.participant_id=a.participant_id "
        "AND sp.study_code=a.study_code "
        "LEFT JOIN experiment_stage_progress progress "
        "ON progress.participation_id=sp.participation_id AND progress.stage_code='questionnaire' "
        "LEFT JOIN experiment_workflow_progress workflow "
        "ON workflow.assignment_id=a.assignment_id AND workflow.step_instance_id=" + marker
        + " WHERE p.participant_code=" + marker
        + " AND a.invitation_token_hash=" + marker + " AND a.active=1"
    )
    row = connection.execute(
        query,
        (
            CONSENT_VERSION,
            step_id,
            normalize_participant_code(participant_code),
            token_hash,
        ),
    ).fetchone()
    if not row:
        raise HTTPException(403, "کد شرکت‌کننده یا لینک اختصاصی معتبر نیست.")
    if not bool(row["consent_accepted"]):
        raise HTTPException(403, "رضایت برای مشارکت ثبت نشده است.")
    return dict(row)


def _public_stage_state(stage_code: str, row: dict[str, Any] | None) -> dict[str, Any]:
    if not row:
        return {
            "stage_code": stage_code,
            "status": "not_started",
            "attempt_number": 0,
            "current_run_id": None,
            "draft_payload": {},
            "started_at": None,
            "completed_at": None,
        }
    state = {
        "stage_code": stage_code,
        "status": row["status"],
        "attempt_number": row["attempt_number"],
        "current_run_id": row["current_run_id"],
        "started_at": row["started_at"],
        "completed_at": row["completed_at"],
    }
    # Only questionnaire drafts are meant to be restored into the interface.
    if stage_code == "questionnaire":
        state["draft_payload"] = _json_value(row.get("draft_payload"))
    return state


@app.get("/api/admin/workflow")
def get_admin_workflow(x_admin_token: str | None = Header(default=None)):
    authorize(x_admin_token)
    with conn() as connection:
        return {"steps": _load_study_workflow(connection, STUDY_CODE)}


@app.post("/api/admin/workflow")
def save_admin_workflow(
    data: WorkflowConfigIn,
    x_admin_token: str | None = Header(default=None),
):
    authorize(x_admin_token)
    steps = _normalize_workflow_steps(data.steps)
    workflow_json = json.dumps(steps, ensure_ascii=False, separators=(",", ":"))
    timestamp = now()
    with conn() as connection:
        if DATABASE_URL:
            connection.execute(
                "INSERT INTO study_workflows(study_code,workflow_json,updated_at) VALUES(%s,%s,%s) "
                "ON CONFLICT(study_code) DO UPDATE SET workflow_json=EXCLUDED.workflow_json,updated_at=EXCLUDED.updated_at",
                (STUDY_CODE, workflow_json, timestamp),
            )
        else:
            connection.execute(
                "INSERT INTO study_workflows(study_code,workflow_json,updated_at) VALUES(?,?,?) "
                "ON CONFLICT(study_code) DO UPDATE SET workflow_json=excluded.workflow_json,updated_at=excluded.updated_at",
                (STUDY_CODE, workflow_json, timestamp),
            )
    return {"saved": True, "steps": steps}


@app.post("/api/admin/assignments")
def create_assignments(
    data: AssignmentBatchIn,
    request: Request,
    x_admin_token: str | None = Header(default=None),
):
    """Create fixed per-participant assignments and one-time-displayed invite links."""
    authorize(x_admin_token)
    created = []
    seen_codes = set()
    base_url = _invitation_base_url(request, "/api/admin/assignments")
    joiner = "&" if "?" in base_url else "?"
    with conn() as workflow_connection:
        workflow_steps = _normalize_workflow_steps(data.steps) if data.steps else _load_study_workflow(workflow_connection, STUDY_CODE)
    with conn() as connection:
        for item in data.assignments:
            code = normalize_participant_code(item.participant_code)
            assignment_steps = [dict(step) for step in workflow_steps]
            condition = _condition_group(item.condition_code) if item.condition_code else _workflow_condition_code(assignment_steps)
            variant = _workflow_ultimatum_variant(assignment_steps)
            workflow_json = json.dumps(assignment_steps, ensure_ascii=False, separators=(",", ":"))
            if code in seen_codes:
                raise HTTPException(422, "Each participant code may appear only once in a batch")
            seen_codes.add(code)
            participant_id, _participation_id = _ensure_participation(connection, code)
            marker = "%s" if DATABASE_URL else "?"
            existing = connection.execute(
                "SELECT assignment_id FROM experiment_assignments "
                "WHERE study_code=" + marker + " AND participant_id=" + marker,
                (STUDY_CODE, participant_id),
            ).fetchone()
            if existing:
                raise HTTPException(409, "An assignment already exists for one of the supplied participant codes")
            invitation_token = secrets.token_urlsafe(32)
            token_hash = hashlib.sha256(invitation_token.encode("utf-8")).hexdigest()
            token_encrypted = _encrypt_invitation_token(invitation_token)
            values = (
                str(uuid.uuid4()), STUDY_CODE, participant_id, condition, token_hash, token_encrypted, 1, now(), workflow_json
            )
            if DATABASE_URL:
                connection.execute(
                    "INSERT INTO experiment_assignments(assignment_id,study_code,participant_id,condition_code,"
                    "invitation_token_hash,invitation_token_encrypted,active,assigned_at,workflow_json) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    values,
                )
            else:
                connection.execute(
                    "INSERT INTO experiment_assignments(assignment_id,study_code,participant_id,condition_code,"
                    "invitation_token_hash,invitation_token_encrypted,active,assigned_at,workflow_json) VALUES(?,?,?,?,?,?,?,?,?)",
                    values,
                )
            created.append({
                "participant_code": code,
                "condition_code": condition,
                "ultimatum_variant": variant,
                "invitation_url": base_url + joiner + "invite=" + invitation_token,
            })
    return {"created_count": len(created), "assignments": created}


@app.post("/api/admin/group-links")
def create_group_link(
    data: GroupLinkCreateIn,
    request: Request,
    x_admin_token: str | None = Header(default=None),
):
    """Create one shareable invitation that issues an individual assignment on first claim."""
    authorize(x_admin_token)
    generation = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S") + "-" + secrets.token_hex(3)
    if data.scope == "test":
        study_code = f"{TEST_STUDY_PREFIX}group-{generation}"
        study_name = f"BarSan group invitation test — {generation}"
    else:
        study_code = STUDY_CODE
        study_name = STUDY_NAME

    with conn() as workflow_connection:
        workflow_steps = (
            _normalize_workflow_steps(data.steps)
            if data.steps
            else _load_study_workflow(workflow_connection, STUDY_CODE)
        )
    workflow_json = json.dumps(workflow_steps, ensure_ascii=False, separators=(",", ":"))
    condition = _workflow_condition_code(workflow_steps)
    token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    token_encrypted = _encrypt_invitation_token(token)
    group_link_id = str(uuid.uuid4())
    label = (data.label or "").strip() or ("لینک گروهی تست" if data.scope == "test" else "لینک گروهی")
    timestamp = now()
    marker = "%s" if DATABASE_URL else "?"

    with conn() as connection:
        if data.scope == "test":
            if DATABASE_URL:
                connection.execute(
                    "INSERT INTO studies(study_code,study_name,created_at) VALUES(%s,%s,%s) "
                    "ON CONFLICT(study_code) DO NOTHING",
                    (study_code, study_name, timestamp),
                )
            else:
                connection.execute(
                    "INSERT OR IGNORE INTO studies(study_code,study_name,created_at) VALUES(?,?,?)",
                    (study_code, study_name, timestamp),
                )
        connection.execute(
            "INSERT INTO experiment_group_links("
            "group_link_id,study_code,label,invitation_token_hash,invitation_token_encrypted,"
            "condition_code,workflow_json,active,created_at) VALUES("
            + ",".join([marker] * 9) + ")",
            (
                group_link_id, study_code, label, token_hash, token_encrypted,
                condition, workflow_json, 1, timestamp,
            ),
        )

    base_url = _invitation_base_url(request, "/api/admin/group-links")
    joiner = "&" if "?" in base_url else "?"
    display_steps, workflow_summary = _group_workflow_details(workflow_steps)
    return {
        "group_link_id": group_link_id,
        "scope": data.scope,
        "label": label,
        "study_code": study_code,
        "condition_code": condition,
        "created_at": timestamp,
        "workflow_steps": display_steps,
        "workflow_summary": workflow_summary,
        "workflow_count": len(display_steps),
        "invitation_url": base_url + joiner + "group=" + token,
        "note": "این لینک برای اشتراک‌گذاری است؛ هر شرکت‌کننده پس از ورود، کد خودش یا کد تصادفی مستقل دریافت می‌کند.",
    }


def _group_workflow_details(raw_steps: Any) -> tuple[list[dict[str, Any]], str]:
    """Return human-readable stage labels for admin display without exposing participant data."""
    if isinstance(raw_steps, str):
        try:
            raw_steps = json.loads(raw_steps)
        except (TypeError, ValueError):
            raw_steps = None
    try:
        steps = _normalize_workflow_steps(raw_steps)
    except (TypeError, ValueError, HTTPException):
        steps = _default_workflow_steps()
    titles = {
        "questionnaire": "پرسشنامه",
        "dd_lifetrade": "بازی انتخاب",
        "dd_standalone": "بازی انتخاب · ۵ دور",
        "intermission": "استراحت بین بازی‌ها",
        "ultimatum": "بازی باشگاه رانندگان بارسان",
        "ultimatum_frame": "بازی باشگاه رانندگان بارسان · Frame",
        "ultimatum_control": "بازی باشگاه رانندگان بارسان · Control",
    }
    display_steps = [
        {
            "step_order": index + 1,
            "module_code": step["module_code"],
            "title": titles.get(step["module_code"], step["module_code"]),
        }
        for index, step in enumerate(steps)
    ]
    return display_steps, " ← ".join(step["title"] for step in display_steps)


@app.get("/api/admin/group-links")
def list_admin_group_links(
    request: Request,
    scope: Literal["main", "test"] = "main",
    x_admin_token: str | None = Header(default=None),
):
    authorize(x_admin_token)
    marker = "%s" if DATABASE_URL else "?"
    if scope == "main":
        where = "gl.study_code=" + marker
        params = (STUDY_CODE,)
    else:
        where = "gl.study_code LIKE " + marker
        params = (TEST_STUDY_PREFIX + "%",)

    with conn() as connection:
        rows = connection.execute(
            "SELECT gl.group_link_id,gl.study_code,gl.label,gl.invitation_token_encrypted,"
            "gl.workflow_json,gl.active,gl.created_at,COUNT(ea.assignment_id) AS claimed_count "
            "FROM experiment_group_links gl "
            "LEFT JOIN experiment_assignments ea ON ea.group_link_id=gl.group_link_id "
            "WHERE " + where + " AND gl.deleted_at IS NULL "
            "GROUP BY gl.group_link_id,gl.study_code,gl.label,gl.invitation_token_encrypted,"
            "gl.workflow_json,gl.active,gl.created_at "
            "ORDER BY gl.created_at DESC",
            params,
        ).fetchall()

    base_url = _invitation_base_url(request, "/api/admin/group-links")
    joiner = "&" if "?" in base_url else "?"
    items = []
    for row in rows:
        token = _decrypt_invitation_token(row["invitation_token_encrypted"])
        workflow_steps, workflow_summary = _group_workflow_details(row["workflow_json"])
        items.append({
            "group_link_id": row["group_link_id"],
            "study_code": row["study_code"],
            "label": row["label"],
            "active": bool(row["active"]),
            "created_at": row["created_at"],
            "claimed_count": int(row["claimed_count"] or 0),
            "workflow_steps": workflow_steps,
            "workflow_summary": workflow_summary,
            "workflow_count": len(workflow_steps),
            "url_available": bool(token),
            "invitation_url": base_url + joiner + "group=" + token if token else "",
        })
    return {"scope": scope, "total": len(items), "items": items}


def _lock_admin_ids(connection, table, id_column, ids, scope):
    """Validate the entire scoped selection before any mutation; stable lock order."""
    if len(set(ids)) != len(ids):
        raise HTTPException(422, "شناسه‌های انتخاب‌شده نباید تکراری باشند.")
    marker = "%s" if DATABASE_URL else "?"
    condition, scope_params = _scope_filter(scope, "study_code", marker)
    if table == "experiment_group_links":
        condition += " AND deleted_at IS NULL"
    selected = []
    ordered = sorted(ids)
    for start in range(0, len(ordered), 200):
        batch = ordered[start:start + 200]
        rows = connection.execute(
            "SELECT " + id_column + " FROM " + table + " WHERE " + condition
            + " AND " + id_column + " IN (" + ",".join([marker] * len(batch)) + ")"
            + " ORDER BY " + id_column + (" FOR UPDATE" if DATABASE_URL else ""),
            (*scope_params, *batch),
        ).fetchall()
        selected.extend(row[id_column] for row in rows)
    if len(selected) != len(ids):
        raise HTTPException(404, "بعضی لینک‌ها حذف شده‌اند یا در محدودهٔ انتخاب‌شده نیستند؛ فهرست را به‌روز کنید.")
    return selected


@app.post("/api/admin/group-links/status")
def set_admin_group_links_status(data: GroupLinkStatusBatchIn, x_admin_token: str | None = Header(default=None)):
    authorize(x_admin_token)
    if data.active and data.include_assignments:
        raise HTTPException(422, "فعال‌سازی لینک‌های افراد باید جداگانه انجام شود.")
    marker = "%s" if DATABASE_URL else "?"
    assignment_count = 0
    with conn() as connection:
        if not DATABASE_URL:
            connection.execute("BEGIN IMMEDIATE")
        ids = _lock_admin_ids(connection, "experiment_group_links", "group_link_id", data.group_link_ids, data.scope)
        for group_id in ids:
            connection.execute("UPDATE experiment_group_links SET active=" + marker + " WHERE group_link_id=" + marker, (int(data.active), group_id))
            if data.include_assignments:
                cursor = connection.execute("UPDATE experiment_assignments SET active=0 WHERE group_link_id=" + marker + " AND active=1", (group_id,))
                assignment_count += max(0, int(cursor.rowcount or 0))
    return {"updated_count": len(ids), "active": data.active, "scope": data.scope, "assignments_deactivated": assignment_count}


@app.post("/api/admin/group-links/delete")
def delete_admin_group_links(data: GroupLinkDeleteIn, x_admin_token: str | None = Header(default=None)):
    """Retire shareable URLs while preserving invitation and group provenance."""
    authorize(x_admin_token)
    if data.confirm_phrase != "DELETE GROUP LINKS":
        raise HTTPException(422, "برای حذف لینک گروهی عبارت DELETE GROUP LINKS را دقیق وارد کنید.")
    marker = "%s" if DATABASE_URL else "?"
    condition, scope_params = _scope_filter(data.scope, "study_code", marker)
    deleted = preserved = 0
    if len(set(data.group_link_ids)) != len(data.group_link_ids):
        raise HTTPException(422, "شناسه‌های انتخاب‌شده نباید تکراری باشند.")
    with conn() as connection:
        if not DATABASE_URL:
            connection.execute("BEGIN IMMEDIATE")
        for start in range(0, len(data.group_link_ids), 200):
            batch = sorted(data.group_link_ids)[start:start + 200]
            rows = connection.execute("SELECT group_link_id FROM experiment_group_links WHERE " + condition
                + " AND deleted_at IS NULL AND group_link_id IN (" + ",".join([marker] * len(batch)) + ") ORDER BY group_link_id"
                + (" FOR UPDATE" if DATABASE_URL else ""), (*scope_params, *batch)).fetchall()
            for row in rows:
                params = (row["group_link_id"],)
                preserved += int(connection.execute("SELECT COUNT(*) AS n FROM experiment_assignments WHERE group_link_id=" + marker, params).fetchone()["n"])
                connection.execute("UPDATE experiment_group_links SET active=0,deleted_at=" + marker + " WHERE group_link_id=" + marker, (now(), row["group_link_id"]))
                deleted += 1
    return {"deleted_count": deleted, "preserved_assignments": preserved, "scope": data.scope, "research_data_preserved": True}


@app.post("/api/admin/test-links")
def create_test_links(
    request: Request,
    data: WorkflowConfigIn | None = None,
    x_admin_token: str | None = Header(default=None),
):
    """Create one isolated test invitation that runs the exact configured workflow."""
    authorize(x_admin_token)
    generation = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S") + "-" + secrets.token_hex(3)
    study_code = f"{TEST_STUDY_PREFIX}{generation}"
    study_name = f"BarSan workflow test — {generation}"
    participant_code = "TEST"
    base_url = _invitation_base_url(request, "/api/admin/test-links")
    joiner = "&" if "?" in base_url else "?"
    with conn() as workflow_connection:
        workflow_steps = (
            _normalize_workflow_steps(data.steps)
            if data and data.steps
            else _load_study_workflow(workflow_connection, STUDY_CODE)
        )
    workflow_json = json.dumps(workflow_steps, ensure_ascii=False, separators=(",", ":"))
    condition = _workflow_condition_code(workflow_steps)
    variant = _workflow_ultimatum_variant(workflow_steps)
    with conn() as connection:
        participant_id, _participation_id = _ensure_participation(
            connection,
            participant_code,
            study_code=study_code,
            study_name=study_name,
        )
        marker = "%s" if DATABASE_URL else "?"
        invitation_token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(invitation_token.encode("utf-8")).hexdigest()
        token_encrypted = _encrypt_invitation_token(invitation_token)
        values = (
            str(uuid.uuid4()), study_code, participant_id, condition, token_hash,
            token_encrypted, 1, now(), workflow_json,
        )
        connection.execute(
            "INSERT INTO experiment_assignments(assignment_id,study_code,participant_id,condition_code,"
            "invitation_token_hash,invitation_token_encrypted,active,assigned_at,workflow_json) VALUES("
            + ",".join([marker] * 9) + ")",
            values,
        )
    invitation_url = base_url + joiner + "invite=" + invitation_token
    return {
        "test_code": participant_code,
        "generation": generation,
        "created_count": 1,
        "workflow_modules": [step["module_code"] for step in workflow_steps],
        "ultimatum_variant": variant,
        "assignments": [{
            "participant_code": participant_code,
            "condition_code": condition,
            "ultimatum_variant": variant,
            "study_code": study_code,
            "invitation_url": invitation_url,
        }],
        "note": "این لینک آزمایشی یک نسخهٔ مستقل از کل روند ذخیره‌شده را اجرا می‌کند؛ مراحل و ترتیب آن با روند اصلی یکسان است.",
    }


@app.post("/api/admin/module-test-links")
def create_module_test_link(
    data: ModuleTestLinkIn,
    request: Request,
    x_admin_token: str | None = Header(default=None),
):
    """Create a single-module-only test link; prerequisites are synthetic and explicitly flagged."""
    authorize(x_admin_token)
    stage_index = STAGE_ORDER.index(data.stage_code)
    generation = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S") + "-" + secrets.token_hex(3)
    study_code = f"{TEST_STUDY_PREFIX}module-{generation}-{data.stage_code}"
    study_name = f"BarSan module test — {data.stage_code} — {generation}"
    participant_code = "TEST"
    base_url = _invitation_base_url(request, "/api/admin/module-test-links")
    joiner = "&" if "?" in base_url else "?"
    with conn() as connection:
        participant_id, participation_id = _ensure_participation(
            connection, participant_code, study_code=study_code, study_name=study_name
        )
        marker = "%s" if DATABASE_URL else "?"
        invitation_token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(invitation_token.encode("utf-8")).hexdigest()
        token_encrypted = _encrypt_invitation_token(invitation_token)
        connection.execute(
            "INSERT INTO experiment_assignments(assignment_id,study_code,participant_id,condition_code,"
            "invitation_token_hash,invitation_token_encrypted,active,assigned_at,workflow_json) VALUES(" + ",".join([marker] * 9) + ")",
            (str(uuid.uuid4()), study_code, participant_id, data.condition_code, token_hash, token_encrypted, 1, now(),
             json.dumps([{"step_id": "module-test-" + data.stage_code, "module_code": data.stage_code}])),
        )
        for prerequisite in STAGE_ORDER[:stage_index]:
            _seed_completed_test_stage(connection, participation_id, prerequisite)
    return {
        "participant_code": participant_code,
        "condition_code": data.condition_code,
        "group": "Frame" if data.condition_code == "A" else "Control",
        "stage_code": data.stage_code,
        "study_code": study_code,
        "invitation_url": base_url + joiner + "invite=" + invitation_token,
        "test_code": "TEST",
        "prerequisites_marked_synthetic": list(STAGE_ORDER[:stage_index]),
    }



@app.get("/api/admin/assignments")
def list_admin_assignments(
    request: Request,
    x_admin_token: str | None = Header(default=None),
    scope: Literal["main", "test"] = "main",
    status: Literal["active", "inactive", "all"] = "all",
    search: str = "",
):
    """List invitation assignments and counts, exposing URLs only to an authenticated admin."""
    authorize(x_admin_token)
    marker = "%s" if DATABASE_URL else "?"
    scope_condition, scope_params = _scope_filter(scope, "a.study_code", marker)
    where = [scope_condition]
    params: list[Any] = list(scope_params)
    if status != "all":
        where.append("a.active=" + marker)
        params.append(1 if status == "active" else 0)
    normalized_search = search.strip().lower()
    if normalized_search:
        where.append("LOWER(p.participant_code) LIKE " + marker)
        params.append("%" + normalized_search + "%")
    query = (
        "SELECT a.assignment_id,a.study_code,p.participant_code,a.condition_code,a.workflow_json,"
        "a.active,a.assigned_at,a.invitation_token_encrypted "
        "FROM experiment_assignments a JOIN participants p ON p.participant_id=a.participant_id "
        "WHERE " + " AND ".join(where) + " ORDER BY a.assigned_at DESC,p.participant_code"
    )
    count_query = (
        "SELECT COALESCE(SUM(CASE WHEN a.active=1 THEN 1 ELSE 0 END),0) AS active_count,"
        "COALESCE(SUM(CASE WHEN a.active=0 THEN 1 ELSE 0 END),0) AS inactive_count "
        "FROM experiment_assignments a WHERE " + scope_condition
    )
    with conn() as connection:
        rows = connection.execute(query, tuple(params)).fetchall()
        counts = connection.execute(count_query, scope_params).fetchone()
    base_url = _invitation_base_url(request, "/api/admin/assignments")
    result_rows = []
    for raw in rows:
        item = dict(raw)
        token = _decrypt_invitation_token(item.pop("invitation_token_encrypted", None))
        item["active"] = bool(item["active"])
        saved_workflow = _workflow_for_assignment(item)
        item["workflow_modules"] = [step["module_code"] for step in saved_workflow]
        item["ultimatum_variant"] = _workflow_ultimatum_variant(saved_workflow)
        item.pop("workflow_json", None)
        item["url_available"] = bool(token)
        item["invitation_url"] = (
            base_url + ("&" if "?" in base_url else "?") + "invite=" + token if token else ""
        )
        result_rows.append(item)
    return {
        "scope": scope,
        "status_filter": status,
        "counts": {
            "active": int(counts["active_count"] or 0),
            "inactive": int(counts["inactive_count"] or 0),
            "total": int(counts["active_count"] or 0) + int(counts["inactive_count"] or 0),
        },
        "items": result_rows,
        "legacy_token_count": sum(not item["url_available"] for item in result_rows),
    }


@app.post("/api/admin/assignments/status")
def set_admin_assignments_status(
    data: AssignmentStatusBatchIn,
    x_admin_token: str | None = Header(default=None),
):
    """Activate/deactivate selected links without deleting their associated research data."""
    authorize(x_admin_token)
    marker = "%s" if DATABASE_URL else "?"
    with conn() as connection:
        if not DATABASE_URL:
            connection.execute("BEGIN IMMEDIATE")
        ids = _lock_admin_ids(connection, "experiment_assignments", "assignment_id", data.assignment_ids, data.scope)
        for start in range(0, len(ids), 200):
            batch = ids[start:start + 200]
            connection.execute("UPDATE experiment_assignments SET active=" + marker + " WHERE assignment_id IN ("
                + ",".join([marker] * len(batch)) + ")", (int(data.active), *batch))
    return {"updated_count": len(ids), "active": data.active, "scope": data.scope,
            "message": "وضعیت لینک‌ها تغییر کرد؛ داده‌های پژوهشی مرتبط دست‌نخورده ماندند."}


@app.post("/api/admin/assignments/reissue-missing-links")
def reissue_missing_admin_assignment_links(
    data: AssignmentIdBatchIn,
    x_admin_token: str | None = Header(default=None),
):
    """Regenerate only selected URLs whose tokens are not recoverable; preserve active status."""
    authorize(x_admin_token)
    if len(set(data.assignment_ids)) != len(data.assignment_ids):
        raise HTTPException(422, "فهرست شناسه‌های لینک نباید تکراری باشد.")
    marker = "%s" if DATABASE_URL else "?"
    scope_condition, scope_params = _scope_filter(data.scope, "a.study_code", marker)
    reissued_count = 0
    skipped_count = 0
    with conn() as connection:
        for start in range(0, len(data.assignment_ids), 400):
            batch_ids = data.assignment_ids[start:start + 400]
            placeholders = ",".join([marker] * len(batch_ids))
            rows = connection.execute(
                "SELECT a.assignment_id,a.invitation_token_encrypted "
                "FROM experiment_assignments a WHERE a.assignment_id IN ("
                + placeholders + ") AND " + scope_condition,
                (*batch_ids, *scope_params),
            ).fetchall()
            for row in rows:
                if _decrypt_invitation_token(row["invitation_token_encrypted"]):
                    skipped_count += 1
                    continue
                new_token = secrets.token_urlsafe(32)
                token_hash = hashlib.sha256(new_token.encode("utf-8")).hexdigest()
                token_encrypted = _encrypt_invitation_token(new_token)
                connection.execute(
                    "UPDATE experiment_assignments SET invitation_token_hash=" + marker
                    + ",invitation_token_encrypted=" + marker + " WHERE assignment_id=" + marker,
                    (token_hash, token_encrypted, row["assignment_id"]),
                )
                reissued_count += 1
    return {
        "reissued_count": reissued_count,
        "skipped_count": skipped_count,
        "scope": data.scope,
        "message": "فقط لینک‌های بدون URL قابل بازیابی، با حفظ وضعیت فعال/غیرفعال، دوباره صادر شدند.",
        "warning": "لینک قدیمی مواردی که دوباره صادر شدند باطل است.",
    }


@app.post("/api/admin/assignments/reissue-link")
def reissue_admin_assignment_link(
    data: AssignmentReissueIn,
    request: Request,
    x_admin_token: str | None = Header(default=None),
):
    """Issue a replacement URL for an assignment whose original token cannot be recovered."""
    authorize(x_admin_token)
    marker = "%s" if DATABASE_URL else "?"
    scope_condition, scope_params = _scope_filter(data.scope, "a.study_code", marker)
    with conn() as connection:
        row = connection.execute(
            "SELECT a.assignment_id,a.study_code,p.participant_code,a.condition_code,a.workflow_json,a.active "
            "FROM experiment_assignments a JOIN participants p ON p.participant_id=a.participant_id "
            "WHERE a.assignment_id=" + marker + " AND " + scope_condition,
            (data.assignment_id, *scope_params),
        ).fetchone()
        if not row:
            raise HTTPException(404, "لینک در محدودهٔ انتخاب‌شده پیدا نشد.")
        new_token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(new_token.encode("utf-8")).hexdigest()
        token_encrypted = _encrypt_invitation_token(new_token)
        connection.execute(
            "UPDATE experiment_assignments SET invitation_token_hash=" + marker
            + ",invitation_token_encrypted=" + marker + " WHERE assignment_id=" + marker,
            (token_hash, token_encrypted, data.assignment_id),
        )
    return {
        "assignment_id": data.assignment_id,
        "participant_code": row["participant_code"],
        "condition_code": row["condition_code"],
        "ultimatum_variant": _workflow_ultimatum_variant(_workflow_for_assignment(dict(row))),
        "active": bool(row["active"]),
        "study_code": row["study_code"],
        "invitation_url": _invitation_url_for_token(request, "/api/admin/assignments/reissue-link", new_token),
        "warning": "لینک قبلی باطل شد؛ وضعیت فعال/غیرفعال مانند قبل باقی ماند.",
    }


@app.post("/api/admin/assignments/delete-inactive")
def delete_inactive_admin_assignments(
    data: InactiveAssignmentDeleteIn,
    x_admin_token: str | None = Header(default=None),
):
    """Delete only inactive invitation assignments in an explicitly selected study scope."""
    authorize(x_admin_token)
    if data.confirm_phrase != "DELETE INACTIVE LINKS":
        raise HTTPException(422, "برای حذف لینک‌های غیرفعال عبارت DELETE INACTIVE LINKS را دقیق وارد کن.")
    marker = "%s" if DATABASE_URL else "?"
    scope_condition, scope_params = _scope_filter(data.scope, "study_code", marker)
    selection_params = ()
    if data.assignment_ids is not None:
        if len(set(data.assignment_ids)) != len(data.assignment_ids):
            raise HTTPException(422, "شناسه‌های انتخاب‌شده نباید تکراری باشند.")
        selection_params = tuple(sorted(data.assignment_ids))
    with conn() as connection:
        if not DATABASE_URL:
            connection.execute("BEGIN IMMEDIATE")
        targets = connection.execute(
            "SELECT assignment_id FROM experiment_assignments WHERE active=0 AND "
            + scope_condition + " ORDER BY assignment_id" + (" FOR UPDATE" if DATABASE_URL else ""),
            scope_params,
        ).fetchall()
        if data.assignment_ids is not None:
            selected_ids = set(selection_params)
            targets = [row for row in targets if row["assignment_id"] in selected_ids]
        if not targets:
            return {
                "deleted_count": 0,
                "scope": data.scope,
                "message": "در محدودهٔ انتخاب‌شده لینک غیرفعالی برای حذف وجود ندارد.",
            }
        deleted_count = 0
        # Freeze the selected IDs: concurrent activation cannot change the target set
        # between child cleanup and parent deletion. Research records stay intact.
        for offset in range(0, len(targets), 200):
            ids = tuple(row["assignment_id"] for row in targets[offset:offset + 200])
            placeholders = ",".join([marker] * len(ids))
            where = "assignment_id IN (" + placeholders + ")"
            # Keep this request ID/fingerprint as a tombstone. Replaying an allocation
            # whose invitation was deleted must fail instead of allocating a new code.
            connection.execute(
                "UPDATE experiment_group_claim_requests SET assignment_id=NULL WHERE " + where,
                ids,
            )
            for table in (
                "experiment_stage_start_requests", "experiment_time_event_requests",
                "experiment_time_tracking", "experiment_workflow_progress", "experiment_consents",
            ):
                connection.execute("DELETE FROM " + table + " WHERE " + where, ids)
            cursor = connection.execute(
                "DELETE FROM experiment_assignments WHERE active=0 AND " + where, ids,
            )
            deleted_count += max(0, int(cursor.rowcount or 0))
    return {
        "deleted_count": deleted_count,
        "scope": data.scope,
        "message": "لینک‌های غیرفعال حذف شدند؛ داده‌های پژوهشی و پاسخ‌های قبلی پاک نشدند.",
    }


@app.get("/api/admin/assignments/export.csv")
def export_admin_active_assignments_csv(
    request: Request,
    x_admin_token: str | None = Header(default=None),
    scope: Literal["main", "test"] = "main",
):
    """Export active assignments; legacy URLs are marked unavailable rather than fabricated."""
    authorize(x_admin_token)
    marker = "%s" if DATABASE_URL else "?"
    scope_condition, scope_params = _scope_filter(scope, "a.study_code", marker)
    query = (
        "SELECT a.assignment_id,a.study_code,p.participant_code,a.condition_code,a.workflow_json,"
        "a.assigned_at,a.invitation_token_encrypted "
        "FROM experiment_assignments a JOIN participants p ON p.participant_id=a.participant_id "
        "WHERE a.active=1 AND " + scope_condition
        + " ORDER BY a.assigned_at,p.participant_code"
    )
    with conn() as connection:
        rows = connection.execute(query, scope_params).fetchall()
    base_url = _invitation_base_url(request, "/api/admin/assignments/export.csv")
    output = io.StringIO(newline="")
    output.write("\ufeff")
    fields = [
        "participant_code", "condition_code", "ultimatum_variant", "active", "study_code",
        "assigned_at", "invitation_url", "url_available", "link_id",
    ]
    writer = csv.DictWriter(output, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    for raw in rows:
        item = dict(raw)
        token = _decrypt_invitation_token(item.get("invitation_token_encrypted"))
        saved_steps = _workflow_for_assignment(item)
        variant = _workflow_ultimatum_variant(saved_steps) or ""
        writer.writerow({
            "participant_code": item["participant_code"],
            "condition_code": item["condition_code"],
            "ultimatum_variant": variant,
            "active": True,
            "study_code": item["study_code"],
            "assigned_at": item["assigned_at"],
            "invitation_url": base_url + ("&" if "?" in base_url else "?") + "invite=" + token if token else "",
            "url_available": bool(token),
            "link_id": item["assignment_id"],
        })
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": 'attachment; filename="' + (
                "barsan_test_active_links.csv" if scope == "test" else "barsan_active_links.csv"
            ) + '"',
            "Cache-Control": "no-store",
        },
    )


@app.post("/api/experiment/group/claim")
def claim_group_invitation(data: GroupClaimIn):
    """Exchange a shared group token for one personal participant code and invitation token."""
    supplied_code = str(data.participant_code or "").strip()
    if data.assign_random:
        if supplied_code:
            raise HTTPException(422, "برای دریافت کد تصادفی، فیلد کد شخصی باید خالی باشد.")
        normalized_code = ""
        request_fingerprint = "random"
    else:
        if not supplied_code:
            raise HTTPException(422, "کد شرکت‌کننده را وارد کنید یا دریافت کد تصادفی را انتخاب کنید.")
        normalized_code = normalize_participant_code(supplied_code)
        request_fingerprint = "own:" + normalized_code

    claim_request_id = None
    if data.claim_request_id:
        try:
            claim_request_id = str(uuid.UUID(data.claim_request_id.strip()))
        except (ValueError, AttributeError):
            raise HTTPException(422, "شناسه درخواست لینک گروهی معتبر نیست.") from None

    group_token_hash = hashlib.sha256(data.group_token.strip().encode("utf-8")).hexdigest()
    marker = "%s" if DATABASE_URL else "?"

    try:
        with conn() as connection:
            group = connection.execute(
                "SELECT group_link_id,study_code,label,condition_code,workflow_json,active "
                "FROM experiment_group_links WHERE invitation_token_hash=" + marker + (" FOR SHARE" if DATABASE_URL else ""),
                (group_token_hash,),
            ).fetchone()
            if not group or not bool(group["active"]):
                raise HTTPException(404, "لینک گروهی معتبر نیست یا غیرفعال شده است.")

            # Reserve the client request ID in the same transaction as the
            # assignment. Concurrent replays wait for the first transaction,
            # then receive the exact participant identity and invitation token.
            if claim_request_id:
                reservation = connection.execute(
                    "INSERT INTO experiment_group_claim_requests("
                    "request_id,group_link_id,request_fingerprint,assignment_id,created_at) "
                    "VALUES(" + ",".join([marker] * 5) + ") "
                    "ON CONFLICT(request_id) DO NOTHING",
                    (claim_request_id, group["group_link_id"], request_fingerprint, None, now()),
                )
                if reservation.rowcount != 1:
                    previous_request = connection.execute(
                        "SELECT group_link_id,request_fingerprint,assignment_id "
                        "FROM experiment_group_claim_requests WHERE request_id=" + marker,
                        (claim_request_id,),
                    ).fetchone()
                    if (
                        not previous_request
                        or previous_request["group_link_id"] != group["group_link_id"]
                        or previous_request["request_fingerprint"] != request_fingerprint
                        or not previous_request["assignment_id"]
                    ):
                        raise HTTPException(409, "این شناسه درخواست قبلاً برای عملیات دیگری استفاده شده است.")
                    previous_assignment = connection.execute(
                        "SELECT a.assignment_id,a.group_link_id,a.invitation_token_encrypted,"
                        "a.condition_code,a.active,p.participant_code "
                        "FROM experiment_assignments AS a "
                        "JOIN participants AS p ON p.participant_id=a.participant_id "
                        "WHERE a.assignment_id=" + marker,
                        (previous_request["assignment_id"],),
                    ).fetchone()
                    if (
                        not previous_assignment
                        or previous_assignment["group_link_id"] != group["group_link_id"]
                        or not bool(previous_assignment["active"])
                    ):
                        raise HTTPException(409, "تخصیص قبلی این درخواست دیگر فعال نیست.")
                    previous_token = _decrypt_invitation_token(
                        previous_assignment["invitation_token_encrypted"]
                    )
                    if not previous_token:
                        raise HTTPException(503, "بازیابی لینک اختصاصی موقتاً ممکن نشد.")
                    return {
                        "participant_code": previous_assignment["participant_code"],
                        "invitation_token": previous_token,
                        "condition_code": previous_assignment["condition_code"],
                        "reused": True,
                        "random_code_assigned": data.assign_random,
                    }

            if data.assign_random:
                participant_code = ""
                for _ in range(8):
                    candidate = "RND-" + secrets.token_hex(6).upper()
                    occupied = connection.execute(
                        "SELECT 1 FROM participants WHERE participant_code=" + marker,
                        (candidate,),
                    ).fetchone()
                    if not occupied:
                        participant_code = candidate
                        break
                if not participant_code:
                    raise HTTPException(503, "ساخت کد تصادفی ممکن نشد؛ دوباره تلاش کنید.")
            else:
                participant_code = normalized_code

            participant_id, _participation_id = _ensure_participation(
                connection,
                participant_code,
                study_code=group["study_code"],
                study_name=group["label"],
            )
            existing = connection.execute(
                "SELECT assignment_id,group_link_id,invitation_token_encrypted,active "
                "FROM experiment_assignments WHERE study_code=" + marker
                + " AND participant_id=" + marker,
                (group["study_code"], participant_id),
            ).fetchone()
            if existing:
                if (
                    existing["group_link_id"] == group["group_link_id"]
                    and bool(existing["active"])
                ):
                    previous_token = _decrypt_invitation_token(existing["invitation_token_encrypted"])
                    if previous_token:
                        if claim_request_id:
                            connection.execute(
                                "UPDATE experiment_group_claim_requests SET assignment_id=" + marker
                                + " WHERE request_id=" + marker,
                                (existing["assignment_id"], claim_request_id),
                            )
                        return {
                            "participant_code": participant_code,
                            "invitation_token": previous_token,
                            "condition_code": group["condition_code"],
                            "reused": True,
                            "random_code_assigned": data.assign_random,
                        }
                raise HTTPException(409, "برای این کد قبلاً در این مطالعه لینک اختصاصی صادر شده است.")

            assignment_id = str(uuid.uuid4())
            invitation_token = secrets.token_urlsafe(32)
            token_hash = hashlib.sha256(invitation_token.encode("utf-8")).hexdigest()
            token_encrypted = _encrypt_invitation_token(invitation_token)
            connection.execute(
                "INSERT INTO experiment_assignments("
                "assignment_id,study_code,participant_id,condition_code,invitation_token_hash,"
                "invitation_token_encrypted,active,assigned_at,workflow_json,group_link_id) VALUES("
                + ",".join([marker] * 10) + ")",
                (
                    assignment_id, group["study_code"], participant_id, group["condition_code"],
                    token_hash, token_encrypted, 1, now(), group["workflow_json"], group["group_link_id"],
                ),
            )
            if claim_request_id:
                connection.execute(
                    "UPDATE experiment_group_claim_requests SET assignment_id=" + marker
                    + " WHERE request_id=" + marker,
                    (assignment_id, claim_request_id),
                )
    except Exception as exc:
        if getattr(exc, "sqlstate", None) == "23505" or isinstance(exc, sqlite3.IntegrityError):
            raise HTTPException(409, "این کد هم‌زمان در حال ثبت است یا قبلاً برای این مطالعه استفاده شده است.") from None
        raise

    return {
        "participant_code": participant_code,
        "invitation_token": invitation_token,
        "condition_code": group["condition_code"],
        "reused": False,
        "random_code_assigned": data.assign_random,
    }


@app.post("/api/experiment/consent")
def record_experiment_consent(data: ExperimentConsentIn):
    """Persist acceptance of the current consent text without storing IP or device identity."""
    if not data.accepted:
        raise HTTPException(403, "شرکت در پژوهش تأیید نشده است.")
    with conn() as connection:
        assignment = _lookup_assignment(connection, data.invitation_token, data.participant_code)
        timestamp = now()
        if DATABASE_URL:
            connection.execute(
                "INSERT INTO experiment_consents(assignment_id,consent_version,accepted_at) "
                "VALUES(%s,%s,%s) ON CONFLICT(assignment_id,consent_version) DO NOTHING",
                (assignment["assignment_id"], CONSENT_VERSION, timestamp),
            )
            row = connection.execute(
                "SELECT accepted_at FROM experiment_consents "
                "WHERE assignment_id=%s AND consent_version=%s",
                (assignment["assignment_id"], CONSENT_VERSION),
            ).fetchone()
        else:
            connection.execute(
                "INSERT OR IGNORE INTO experiment_consents(assignment_id,consent_version,accepted_at) "
                "VALUES(?,?,?)",
                (assignment["assignment_id"], CONSENT_VERSION, timestamp),
            )
            row = connection.execute(
                "SELECT accepted_at FROM experiment_consents "
                "WHERE assignment_id=? AND consent_version=?",
                (assignment["assignment_id"], CONSENT_VERSION),
            ).fetchone()
    return {"accepted": True, "consent_version": CONSENT_VERSION, "accepted_at": row["accepted_at"]}


@app.get("/api/experiment/workflow")
def get_public_experiment_workflow(invite: str | None = None, group: str | None = None):
    """Return only the ordered, non-sensitive module outline for a valid active invite."""
    invite_token = str(invite or "").strip()
    group_token = str(group or "").strip()
    if bool(invite_token) == bool(group_token):
        raise HTTPException(404, "لینک دعوت معتبر نیست.")
    token = invite_token or group_token
    if len(token) < 20 or len(token) > 200:
        raise HTTPException(404, "لینک دعوت معتبر نیست.")
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    marker = "%s" if DATABASE_URL else "?"
    with conn() as connection:
        if invite_token:
            assignment = connection.execute(
                "SELECT study_code,condition_code,workflow_json FROM experiment_assignments "
                "WHERE invitation_token_hash=" + marker + " AND active=1",
                (token_hash,),
            ).fetchone()
        else:
            assignment = connection.execute(
                "SELECT study_code,condition_code,workflow_json FROM experiment_group_links "
                "WHERE invitation_token_hash=" + marker + " AND active=1",
                (token_hash,),
            ).fetchone()
    if not assignment:
        raise HTTPException(404, "لینک دعوت معتبر نیست یا غیرفعال شده است.")
    module_only = _test_module_from_study_code(str(assignment["study_code"] or ""))
    if assignment["workflow_json"]:
        raw = assignment["workflow_json"]
        try:
            if isinstance(raw, str):
                raw = json.loads(raw)
            steps = _normalize_workflow_steps(raw)
        except (TypeError, ValueError, HTTPException):
            steps = _default_workflow_steps()
        is_legacy = False
    elif module_only:
        steps = [{"step_id": "module-test-" + module_only, "module_code": module_only}]
        is_legacy = False
    else:
        steps = _default_workflow_steps()
        is_legacy = True
    module_details = {
        "dd_standalone": ("بازی انتخاب", "پنج انتخاب میان دریافت پاداش در زمان‌های متفاوت"),
        "intermission": ("استراحت", "فرصتی برای استراحت و ادامه با آمادگی شما"),
        "questionnaire": (
            "پرسشنامه",
            "چند پرسش دربارهٔ تجربهٔ همکاری و شرایط کاری",
        ),
        "dd_lifetrade": (
            "تکلیف تصمیم‌گیری",
            "انتخاب میان دریافت پاداش در زمان‌های متفاوت",
        ),
        "ultimatum": (
            "بازی اولتیماتوم",
            "تقسیم امتیاز و تصمیم‌گیری در تعامل با بازیکن مقابل",
        ),
        "ultimatum_frame": (
            "بازی اولتیماتوم — Frame",
            "نسخهٔ Frame بازی اولتیماتوم؛ تقسیم امتیاز و تصمیم‌گیری در تعامل با بازیکن مقابل",
        ),
        "ultimatum_control": (
            "بازی اولتیماتوم — Control",
            "نسخهٔ Control بازی اولتیماتوم؛ تقسیم امتیاز و تصمیم‌گیری در تعامل با بازیکن مقابل",
        ),
    }
    totals: dict[str, int] = {}
    for step in steps:
        totals[step["module_code"]] = totals.get(step["module_code"], 0) + 1
    seen: dict[str, int] = {}
    public_steps = []
    for index, step in enumerate(steps):
        code = step["module_code"]
        seen[code] = seen.get(code, 0) + 1
        title, description = module_details[code]
        public_steps.append({
            "step_order": index + 1,
            "module_code": code,
            "title": title,
            "description": description,
            "repeat_number": seen[code],
            "repeat_total": totals[code],
        })
    return {
        "steps": public_steps,
        "total_steps": len(public_steps),
        "legacy_default": is_legacy,
        "module_test_only": module_only,
    }


@app.post("/api/experiment/identify")
def identify_experiment_participant(data: ExperimentIdentityIn):
    """Resolve the assigned link, workflow instances and legacy stage snapshot."""
    with conn() as connection:
        assignment = _lookup_assignment(
            connection, data.invitation_token, data.participant_code, require_consent=True
        )
        rows = _get_stage_rows(connection, assignment["participation_id"])
        saved_steps = _workflow_progress_rows(connection, assignment["assignment_id"])
    module_only = _test_module_from_study_code(assignment["study_code"])
    configured_workflow = bool(assignment.get("workflow_json")) or bool(module_only)
    steps = _workflow_for_assignment(assignment) if configured_workflow else []
    public_steps = []
    for index, step in enumerate(steps):
        saved = saved_steps.get(step["step_id"], {})
        public_steps.append({
            **step,
            "step_order": index + 1,
            "status": saved.get("status", "not_started"),
            "attempt_number": int(saved.get("attempt_number") or 0),
            "current_run_id": saved.get("current_run_id"),
            "draft_payload": _json_value(saved.get("draft_payload")) if step["module_code"] == "questionnaire" else {},
            "started_at": saved.get("started_at"),
            "completed_at": saved.get("completed_at"),
        })
    states = [_public_stage_state(stage, rows.get(stage)) for stage in STAGE_ORDER]
    current_stage = next((state["stage_code"] for state in states if state["status"] != "completed"), None)
    current_step = next((step for step in public_steps if step["status"] != "completed"), None)
    return {
        "participant_code": assignment["participant_code"],
        "assignment_id": assignment["assignment_id"],
        "condition_code": assignment["condition_code"],
        "study_code": assignment["study_code"],
        "test_module_only": module_only,
        "current_stage": current_step["module_code"] if current_step else current_stage,
        "current_step_id": current_step["step_id"] if current_step else None,
        "workflow_steps": public_steps,
        "stages": states,
    }


@app.post("/api/experiment/time/checkpoints")
def track_client_time(data: ClientTimingIn):
    with conn() as connection:
        if not DATABASE_URL:
            connection.execute("BEGIN IMMEDIATE")
        assignment = _lookup_assignment(connection, data.invitation_token, data.participant_code, require_consent=True)
        acknowledged = save_client_timing(connection, assignment, data.checkpoints, bool(DATABASE_URL), _json_value, now())
    return {"saved": True, "acknowledged_checkpoints": acknowledged}


@app.post("/api/experiment/time")
def track_experiment_time(data: ExperimentTimeIn):
    """Update active time atomically and de-duplicate replayed client events."""
    with conn() as connection:
        assignment = _lookup_assignment(
            connection, data.invitation_token, data.participant_code, require_consent=True
        )
        steps = _workflow_for_assignment(assignment)
        step_index = next((i for i, step in enumerate(steps) if step["step_id"] == data.step_id), None)
        if step_index is None or _canonical_workflow_stage(steps[step_index]["module_code"]) != data.stage_code:
            raise HTTPException(422, "این ماژول در روند آزمایش این لینک وجود ندارد.")

        marker = "%s" if DATABASE_URL else "?"
        if data.event_id:
            inserted = connection.execute(
                "INSERT INTO experiment_time_event_requests("
                "event_id,assignment_id,step_instance_id,stage_code,event,created_at) "
                "VALUES(" + ",".join([marker] * 6) + ") ON CONFLICT(event_id) DO NOTHING",
                (
                    data.event_id, assignment["assignment_id"], data.step_id,
                    data.stage_code, data.event, now(),
                ),
            )
            if inserted.rowcount == 0:
                previous = connection.execute(
                    "SELECT assignment_id,step_instance_id,stage_code,event "
                    "FROM experiment_time_event_requests WHERE event_id=" + marker,
                    (data.event_id,),
                ).fetchone()
                if (
                    not previous
                    or previous["assignment_id"] != assignment["assignment_id"]
                    or previous["step_instance_id"] != data.step_id
                    or previous["stage_code"] != data.stage_code
                    or previous["event"] != data.event
                ):
                    raise HTTPException(409, "شناسهٔ رخداد زمان قبلاً برای درخواست دیگری استفاده شده است.")
                snapshot = connection.execute(
                    "SELECT active_seconds,is_active FROM experiment_time_tracking "
                    "WHERE assignment_id=" + marker + " AND step_instance_id=" + marker,
                    (assignment["assignment_id"], data.step_id),
                ).fetchone()
                if not snapshot:
                    raise HTTPException(409, "ثبت زمان این ماژول هنوز آغاز نشده است.")
                return {
                    "saved": True,
                    "duplicate": True,
                    "event_id": data.event_id,
                    "step_id": data.step_id,
                    "event": data.event,
                    "active_seconds": round(float(snapshot["active_seconds"] or 0), 2),
                    "is_active": bool(snapshot["is_active"]),
                }

        timestamp = now()
        is_active = 0 if data.event in {"pause", "complete"} else 1
        completed_at = timestamp if data.event == "complete" else None

        if DATABASE_URL:
            if data.event == "start":
                row = connection.execute(
                    "INSERT INTO experiment_time_tracking(assignment_id,step_instance_id,step_order,stage_code,"
                    "active_seconds,is_active,started_at,last_seen_at,completed_at) "
                    "VALUES(%s,%s,%s,%s,0,1,%s,%s,NULL) "
                    "ON CONFLICT(assignment_id,step_instance_id) DO UPDATE SET "
                    "active_seconds=experiment_time_tracking.active_seconds + CASE "
                    "WHEN experiment_time_tracking.is_active=1 THEN LEAST(45.0,GREATEST(0.0,"
                    "EXTRACT(EPOCH FROM (EXCLUDED.last_seen_at::timestamptz - "
                    "experiment_time_tracking.last_seen_at::timestamptz)))) ELSE 0.0 END,"
                    "is_active=1,last_seen_at=EXCLUDED.last_seen_at "
                    "RETURNING active_seconds,is_active",
                    (assignment["assignment_id"], data.step_id, step_index + 1, data.stage_code, timestamp, timestamp),
                ).fetchone()
            else:
                row = connection.execute(
                    "UPDATE experiment_time_tracking SET "
                    "active_seconds=active_seconds + CASE WHEN is_active=1 THEN "
                    "LEAST(45.0,GREATEST(0.0,EXTRACT(EPOCH FROM "
                    "(" + marker + "::timestamptz - last_seen_at::timestamptz)))) ELSE 0.0 END,"
                    "is_active=" + marker + ",last_seen_at=" + marker + ","
                    "completed_at=COALESCE(" + marker + ",completed_at) "
                    "WHERE assignment_id=" + marker + " AND step_instance_id=" + marker
                    + " RETURNING active_seconds,is_active",
                    (timestamp, is_active, timestamp, completed_at, assignment["assignment_id"], data.step_id),
                ).fetchone()
        else:
            if data.event == "start":
                row = connection.execute(
                    "INSERT INTO experiment_time_tracking(assignment_id,step_instance_id,step_order,stage_code,"
                    "active_seconds,is_active,started_at,last_seen_at,completed_at) "
                    "VALUES(?,?,?,?,0,1,?,?,NULL) "
                    "ON CONFLICT(assignment_id,step_instance_id) DO UPDATE SET "
                    "active_seconds=experiment_time_tracking.active_seconds + CASE "
                    "WHEN experiment_time_tracking.is_active=1 THEN MIN(45.0,MAX(0.0,"
                    "(julianday(excluded.last_seen_at)-julianday(experiment_time_tracking.last_seen_at))*86400.0)) "
                    "ELSE 0.0 END,"
                    "is_active=1,last_seen_at=excluded.last_seen_at "
                    "RETURNING active_seconds,is_active",
                    (assignment["assignment_id"], data.step_id, step_index + 1, data.stage_code, timestamp, timestamp),
                ).fetchone()
            else:
                row = connection.execute(
                    "UPDATE experiment_time_tracking SET "
                    "active_seconds=active_seconds + CASE WHEN is_active=1 THEN "
                    "MIN(45.0,MAX(0.0,(julianday(?) - julianday(last_seen_at))*86400.0)) ELSE 0.0 END,"
                    "is_active=?,last_seen_at=?,completed_at=COALESCE(?,completed_at) "
                    "WHERE assignment_id=? AND step_instance_id=? RETURNING active_seconds,is_active",
                    (timestamp, is_active, timestamp, completed_at, assignment["assignment_id"], data.step_id),
                ).fetchone()

        if not row:
            raise HTTPException(409, "ثبت زمان این ماژول هنوز آغاز نشده است.")
        return {
            "saved": True,
            "duplicate": False,
            "event_id": data.event_id,
            "step_id": data.step_id,
            "event": data.event,
            "active_seconds": round(float(row["active_seconds"] or 0), 2),
            "is_active": bool(row["is_active"]),
        }


@app.post("/api/experiment/stages/start")
def start_experiment_stage(data: StageStartIn):
    """Start a fresh attempt unless an ambiguous transport retry explicitly requests recovery."""
    if data.stage_code == "intermission" and not data.step_id:
        raise HTTPException(422, "استراحت باید بخشی از روند همین لینک باشد.")
    with conn() as connection:
        # Take the write lock before any SQLite read: a deferred read-to-write
        # upgrade can otherwise fail or let two starts observe missing progress.
        if not DATABASE_URL:
            connection.execute("BEGIN IMMEDIATE")
        elif data.start_request_id:
            # Serialize even conflicting use of the same globally unique ID by
            # different assignments. The lock is released by commit/rollback.
            connection.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                ("barsan-stage-start:" + data.start_request_id,),
            )
        assignment = _lookup_assignment(
            connection, data.invitation_token, data.participant_code,
            require_consent=True, lock_for_start=True,
        )
        marker = "%s" if DATABASE_URL else "?"
        if data.start_request_id:
            prior_request = connection.execute(
                "SELECT assignment_id,step_instance_id,stage_code,run_id,attempt_number "
                "FROM experiment_stage_start_requests WHERE request_id=" + marker,
                (data.start_request_id,),
            ).fetchone()
            if prior_request:
                if (
                    prior_request["assignment_id"] != assignment["assignment_id"]
                    or prior_request["step_instance_id"] != data.step_id
                    or prior_request["stage_code"] != data.stage_code
                ):
                    raise HTTPException(409, "شناسهٔ شروع قبلاً برای درخواست دیگری استفاده شده است.")
                run = connection.execute(
                    "SELECT status FROM instrument_runs WHERE run_id=" + marker,
                    (prior_request["run_id"],),
                ).fetchone()
                if not run:
                    raise HTTPException(409, "اجرای ثبت‌شده برای این درخواست دیگر موجود نیست.")
                progress = None
                if data.step_id:
                    progress = connection.execute(
                        "SELECT current_run_id,status,draft_payload FROM experiment_workflow_progress "
                        "WHERE assignment_id=" + marker + " AND step_instance_id=" + marker,
                        (assignment["assignment_id"], data.step_id),
                    ).fetchone()
                else:
                    progress = connection.execute(
                        "SELECT current_run_id,status,draft_payload FROM experiment_stage_progress "
                        "WHERE participation_id=" + marker + " AND stage_code=" + marker,
                        (assignment["participation_id"], data.stage_code),
                    ).fetchone()
                if not progress or progress["current_run_id"] != prior_request["run_id"]:
                    raise HTTPException(409, "این اجرای آزمایش با شروع مجدد جایگزین شده است.")
                return {
                    "stage_code": data.stage_code,
                    "status": progress["status"],
                    "resumed": True,
                    "already_completed": progress["status"] == "completed",
                    "run_id": prior_request["run_id"],
                    "attempt_number": prior_request["attempt_number"],
                    "draft_payload": _json_value(progress["draft_payload"]),
                    "condition_code": assignment["condition_code"],
                    "step_id": data.step_id,
                }

        # Workflow assignments use their own per-step progress rows. Avoid loading
        # legacy stage snapshots on the event path when a step_id is supplied.
        rows = _get_stage_rows(connection, assignment["participation_id"]) if not data.step_id else {}
        workflow_step = None
        workflow_step_index = None
        if data.step_id:
            workflow_steps = _workflow_for_assignment(assignment)
            workflow_step_index = next(
                (index for index, step in enumerate(workflow_steps) if step["step_id"] == data.step_id),
                None,
            )
            if workflow_step_index is None or _canonical_workflow_stage(workflow_steps[workflow_step_index]["module_code"]) != data.stage_code:
                raise HTTPException(422, "این ماژول در روند آزمایش این لینک وجود ندارد.")
            workflow_step = workflow_steps[workflow_step_index]
            saved_steps = _workflow_progress_rows(connection, assignment["assignment_id"])
            for previous_step in workflow_steps[:workflow_step_index]:
                previous_state = saved_steps.get(previous_step["step_id"])
                if not previous_state or previous_state["status"] != "completed":
                    raise HTTPException(409, "ماژول قبلی هنوز تکمیل نشده است.")
            existing = saved_steps.get(data.step_id)
        else:
            existing = rows.get(data.stage_code)

        if existing and existing["status"] == "completed":
            return {
                "stage_code": data.stage_code,
                "status": "completed",
                "already_completed": True,
                "run_id": existing["current_run_id"],
                "attempt_number": existing["attempt_number"],
            }

        # A retry after an ambiguous transport failure must not supersede a run
        # that may already have committed. This flag is intentionally opt-in so
        # an ordinary participant restart retains its established semantics.
        if (
            data.resume_if_active
            and existing
            and existing["status"] == "in_progress"
            and existing.get("current_run_id")
        ):
            return {
                "stage_code": data.stage_code,
                "status": "in_progress",
                "resumed": True,
                "already_completed": False,
                "run_id": existing["current_run_id"],
                "attempt_number": existing["attempt_number"],
                "draft_payload": _json_value(existing.get("draft_payload")),
                "condition_code": assignment["condition_code"],
                "step_id": data.step_id,
            }

        if not data.step_id:
            stage_index = STAGE_ORDER.index(data.stage_code)
            if stage_index > 0:
                previous = STAGE_ORDER[stage_index - 1]
                previous_state = rows.get(previous)
                if not previous_state or previous_state["status"] != "completed":
                    raise HTTPException(409, "مرحلهٔ قبلی هنوز تکمیل نشده است.")

        instrument_code, version, instrument_name = STAGE_TO_INSTRUMENT[data.stage_code]
        attempt_number = int(existing["attempt_number"]) + 1 if existing else 1
        timestamp = now()
        old_run_id = existing["current_run_id"] if existing else None

        if old_run_id:
            marker = "%s" if DATABASE_URL else "?"
            old_run = connection.execute(
                "SELECT metadata FROM instrument_runs WHERE run_id=" + marker,
                (old_run_id,),
            ).fetchone()
            old_metadata = _json_value(old_run["metadata"]) if old_run else {}
            if not isinstance(old_metadata, dict):
                old_metadata = {}
            abandoned_draft = _json_value(existing.get("draft_payload")) if existing else {}
            if abandoned_draft:
                old_metadata["abandoned_draft_payload"] = abandoned_draft
            old_metadata["supersede_reason"] = "participant_reentry"
            connection.execute(
                "UPDATE instrument_runs SET status='superseded',completed_at=COALESCE(completed_at,"
                + marker + "),metadata=" + marker + " WHERE run_id=" + marker,
                (timestamp, _json_param(old_metadata), old_run_id),
            )

        if DATABASE_URL:
            instrument_version = connection.execute(
                "SELECT instrument_version_id FROM instrument_versions WHERE instrument_code=%s AND version=%s",
                (instrument_code, version),
            ).fetchone()
        else:
            instrument_version = connection.execute(
                "SELECT instrument_version_id FROM instrument_versions WHERE instrument_code=? AND version=?",
                (instrument_code, version),
            ).fetchone()
        if not instrument_version:
            raise HTTPException(500, "نسخهٔ ابزار آزمایش در پایگاه داده ثبت نشده است.")

        run_id = str(uuid.uuid4())
        stage_draft: dict[str, Any] = {}
        workflow_module_code = workflow_step["module_code"] if workflow_step else data.stage_code
        if workflow_module_code == "dd_standalone":
            stage_draft["dd_design"] = DD_WORKFLOW_DESIGNS[workflow_module_code]
        if data.stage_code == "ultimatum":
            stage_draft["workflow_module_code"] = workflow_module_code
            stage_draft["ultimatum_variant"] = ULTIMATUM_WORKFLOW_VARIANTS.get(workflow_module_code)
            # Randomize block order server-side and persist it with this attempt.
            stage_draft["block_order"] = random.sample(
                list(ULTIMATUM_BLOCKS.keys()), k=ULTIMATUM_MAIN_BLOCK_COUNT
            )
        metadata = {
            "stage_code": data.stage_code,
            "workflow_module_code": workflow_module_code,
            "ultimatum_variant": ULTIMATUM_WORKFLOW_VARIANTS.get(workflow_module_code),
            "condition_code": assignment["condition_code"],
            "attempt_number": attempt_number,
            "block_order": stage_draft.get("block_order"),
            "dd_design": stage_draft.get("dd_design"),
            "workflow_step_id": data.step_id,
        }
        if DATABASE_URL:
            connection.execute(
                "INSERT INTO instrument_runs(run_id,participation_id,instrument_version_id,started_at,status,metadata) "
                "VALUES(%s,%s,%s,%s,'in_progress',%s)",
                (run_id, assignment["participation_id"], instrument_version["instrument_version_id"],
                 timestamp, Jsonb(metadata)),
            )
            if data.stage_code in STAGE_ORDER:
                connection.execute(
                    "INSERT INTO experiment_stage_progress(participation_id,stage_code,status,attempt_number,current_run_id,"
                    "draft_payload,started_at,completed_at,updated_at) VALUES(%s,%s,'in_progress',%s,%s,%s,%s,NULL,%s) "
                    "ON CONFLICT(participation_id,stage_code) DO UPDATE SET status='in_progress',"
                    "attempt_number=EXCLUDED.attempt_number,current_run_id=EXCLUDED.current_run_id,"
                    "draft_payload=EXCLUDED.draft_payload,started_at=EXCLUDED.started_at,completed_at=NULL,"
                    "updated_at=EXCLUDED.updated_at",
                    (assignment["participation_id"], data.stage_code, attempt_number, run_id,
                     Jsonb(stage_draft), timestamp, timestamp),
                )
        else:
            connection.execute(
                "INSERT INTO instrument_runs(run_id,participation_id,instrument_version_id,started_at,status,metadata) "
                "VALUES(?,?,?,?,?,?)",
                (run_id, assignment["participation_id"], instrument_version["instrument_version_id"],
                 timestamp, "in_progress", json.dumps(metadata, ensure_ascii=False)),
            )
            if data.stage_code in STAGE_ORDER:
                connection.execute(
                    "INSERT INTO experiment_stage_progress(participation_id,stage_code,status,attempt_number,current_run_id,"
                    "draft_payload,started_at,completed_at,updated_at) VALUES(?,?,'in_progress',?,?,?, ?,NULL,?) "
                    "ON CONFLICT(participation_id,stage_code) DO UPDATE SET status='in_progress',"
                    "attempt_number=excluded.attempt_number,current_run_id=excluded.current_run_id,"
                    "draft_payload=excluded.draft_payload,started_at=excluded.started_at,completed_at=NULL,"
                    "updated_at=excluded.updated_at",
                    (assignment["participation_id"], data.stage_code, attempt_number, run_id,
                     json.dumps(stage_draft, ensure_ascii=False), timestamp, timestamp),
                )
        if data.step_id and workflow_step is not None:
            if DATABASE_URL:
                connection.execute(
                    "INSERT INTO experiment_workflow_progress(assignment_id,participation_id,step_instance_id,step_order,"
                    "stage_code,status,attempt_number,current_run_id,draft_payload,started_at,completed_at,updated_at) "
                    "VALUES(%s,%s,%s,%s,%s,'in_progress',%s,%s,%s,%s,NULL,%s) "
                    "ON CONFLICT(assignment_id,step_instance_id) DO UPDATE SET step_order=EXCLUDED.step_order,"
                    "stage_code=EXCLUDED.stage_code,status='in_progress',attempt_number=EXCLUDED.attempt_number,"
                    "current_run_id=EXCLUDED.current_run_id,draft_payload=EXCLUDED.draft_payload,"
                    "started_at=EXCLUDED.started_at,completed_at=NULL,updated_at=EXCLUDED.updated_at",
                    (assignment["assignment_id"], assignment["participation_id"], data.step_id, workflow_step_index + 1,
                     data.stage_code, attempt_number, run_id, Jsonb(stage_draft), timestamp, timestamp),
                )
            else:
                connection.execute(
                    "INSERT INTO experiment_workflow_progress(assignment_id,participation_id,step_instance_id,step_order,"
                    "stage_code,status,attempt_number,current_run_id,draft_payload,started_at,completed_at,updated_at) "
                    "VALUES(?,?,?,?,?,'in_progress',?,?,?,?,NULL,?) "
                    "ON CONFLICT(assignment_id,step_instance_id) DO UPDATE SET step_order=excluded.step_order,"
                    "stage_code=excluded.stage_code,status='in_progress',attempt_number=excluded.attempt_number,"
                    "current_run_id=excluded.current_run_id,draft_payload=excluded.draft_payload,"
                    "started_at=excluded.started_at,completed_at=NULL,updated_at=excluded.updated_at",
                    (assignment["assignment_id"], assignment["participation_id"], data.step_id, workflow_step_index + 1,
                     data.stage_code, attempt_number, run_id, json.dumps(stage_draft, ensure_ascii=False), timestamp, timestamp),
                )
        if data.start_request_id:
            connection.execute(
                "INSERT INTO experiment_stage_start_requests("
                "request_id,assignment_id,step_instance_id,stage_code,run_id,attempt_number,created_at) "
                "VALUES(" + ",".join([marker] * 7) + ")",
                (
                    data.start_request_id, assignment["assignment_id"], data.step_id,
                    data.stage_code, run_id, attempt_number, timestamp,
                ),
            )
    return {
        "stage_code": data.stage_code,
        "status": "in_progress",
        "resumed": False,
        "already_completed": False,
        "run_id": run_id,
        "attempt_number": attempt_number,
        "draft_payload": stage_draft,
        "condition_code": assignment["condition_code"],
        "step_id": data.step_id,
    }


@app.post("/api/experiment/stages/draft")
def save_questionnaire_draft(data: StageDraftIn):
    """Persist draft state within an attempt; re-entry creates a separate fresh attempt."""
    with conn() as connection:
        assignment = _lookup_questionnaire_assignment_state(
            connection, data.invitation_token, data.participant_code, step_id=data.step_id
        )
        marker = "%s" if DATABASE_URL else "?"
        if data.step_id and assignment["workflow_run_id"] != data.run_id:
            raise HTTPException(409, "این اجرای پرسشنامه دیگر فعال نیست.")
        if assignment["questionnaire_run_id"] != data.run_id:
            raise HTTPException(409, "این اجرای پرسشنامه دیگر فعال نیست.")
        if assignment["questionnaire_status"] == "completed":
            raise HTTPException(409, "پرسشنامه قبلاً تکمیل شده است.")
        timestamp = now()
        connection.execute(
            "UPDATE experiment_stage_progress SET draft_payload=" + marker + ",updated_at=" + marker
            + " WHERE participation_id=" + marker + " AND stage_code=" + marker,
            (_json_param(data.draft_payload), timestamp, assignment["participation_id"], data.stage_code),
        )
        if data.step_id:
            connection.execute(
                "UPDATE experiment_workflow_progress SET draft_payload=" + marker + ",updated_at=" + marker
                + " WHERE assignment_id=" + marker + " AND step_instance_id=" + marker + " AND current_run_id=" + marker,
                (_json_param(data.draft_payload), timestamp, assignment["assignment_id"], data.step_id, data.run_id),
            )
    return {"saved": True, "stage_code": data.stage_code, "run_id": data.run_id, "step_id": data.step_id}


def _persist_questionnaire_answer_rows(
    connection: Any,
    assignment: dict[str, Any],
    run_id: str,
    step_id: str | None,
    answers: list[QuestionnaireAnswerBatchItem] | list[QuestionnaireAnswerIn],
) -> list[dict[str, Any]]:
    """Persist a bounded answer batch atomically and refresh the draft once."""
    marker = "%s" if DATABASE_URL else "?"
    timestamp = now()
    saved: list[dict[str, Any]] = []
    for answer in answers:
        if DATABASE_URL:
            connection.execute(
                "INSERT INTO questionnaire_responses(run_id,question_code,response_value,updated_at) "
                "VALUES(%s,%s,%s,%s) ON CONFLICT(run_id,question_code) DO UPDATE SET "
                "response_value=EXCLUDED.response_value,updated_at=EXCLUDED.updated_at",
                (run_id, answer.question_code, answer.response_value, timestamp),
            )
        else:
            connection.execute(
                "INSERT INTO questionnaire_responses(run_id,question_code,response_value,updated_at) "
                "VALUES(?,?,?,?) ON CONFLICT(run_id,question_code) DO UPDATE SET "
                "response_value=excluded.response_value,updated_at=excluded.updated_at",
                (run_id, answer.question_code, answer.response_value, timestamp),
            )
        saved.append({
            "question_code": answer.question_code,
            "response_value": answer.response_value,
            "current_question_index": answer.current_question_index,
        })

    draft = _json_value(assignment["questionnaire_draft_payload"])
    draft_answers = draft.get("answers")
    if not isinstance(draft_answers, dict):
        draft_answers = {}
    for answer in answers:
        draft_answers[answer.question_code] = answer.response_value
    draft["answers"] = draft_answers
    draft["current_question_index"] = answers[-1].current_question_index
    connection.execute(
        "UPDATE experiment_stage_progress SET draft_payload=" + marker + ",updated_at=" + marker
        + " WHERE participation_id=" + marker + " AND stage_code=" + marker,
        (_json_param(draft), timestamp, assignment["participation_id"], "questionnaire"),
    )
    if step_id:
        connection.execute(
            "UPDATE experiment_workflow_progress SET draft_payload=" + marker + ",updated_at=" + marker
            + " WHERE assignment_id=" + marker + " AND step_instance_id=" + marker + " AND current_run_id=" + marker,
            (_json_param(draft), timestamp, assignment["assignment_id"], step_id, run_id),
        )
    return saved


def _validate_questionnaire_answer_codes(answers: list[Any]) -> None:
    codes = [answer.question_code for answer in answers]
    if any(code not in QUESTIONNAIRE_QUESTION_CODES for code in codes):
        raise HTTPException(422, "Unknown questionnaire question code")
    if len(codes) != len(set(codes)):
        raise HTTPException(422, "A questionnaire batch cannot contain duplicate question codes")


@app.post("/api/experiment/questionnaire/answers")
def save_questionnaire_answer_batch(data: QuestionnaireAnswersBatchIn):
    """Atomically upsert up to ten answers while acquiring one DB connection per batch."""
    _validate_questionnaire_answer_codes(data.answers)
    with conn() as connection:
        assignment = _lookup_questionnaire_assignment_state(
            connection, data.invitation_token, data.participant_code, step_id=data.step_id
        )
        if data.step_id and assignment["workflow_run_id"] != data.run_id:
            raise HTTPException(409, "این اجرای پرسشنامه دیگر فعال نیست.")
        if assignment["questionnaire_run_id"] != data.run_id:
            raise HTTPException(409, "این اجرای پرسشنامه دیگر فعال نیست.")
        if assignment["questionnaire_status"] != "in_progress":
            raise HTTPException(409, "پرسشنامه در وضعیت قابل ویرایش نیست.")
        saved = _persist_questionnaire_answer_rows(
            connection, assignment, data.run_id, data.step_id, data.answers
        )
    return {
        "saved": True,
        "saved_count": len(saved),
        "answers": saved,
    }


@app.post("/api/experiment/questionnaire/answer")
def save_questionnaire_answer(data: QuestionnaireAnswerIn):
    """Upsert one answer by its hidden question code and refresh the server-side draft."""
    _validate_questionnaire_answer_codes([data])
    with conn() as connection:
        assignment = _lookup_questionnaire_assignment_state(
            connection, data.invitation_token, data.participant_code, step_id=data.step_id
        )
        if data.step_id and assignment["workflow_run_id"] != data.run_id:
            raise HTTPException(409, "این اجرای پرسشنامه دیگر فعال نیست.")
        if assignment["questionnaire_run_id"] != data.run_id:
            raise HTTPException(409, "این اجرای پرسشنامه دیگر فعال نیست.")
        if assignment["questionnaire_status"] != "in_progress":
            raise HTTPException(409, "پرسشنامه در وضعیت قابل ویرایش نیست.")
        saved = _persist_questionnaire_answer_rows(
            connection, assignment, data.run_id, data.step_id, [data]
        )[0]
    return {"saved": True, **saved}

def _lookup_active_ultimatum_stage(
    connection: Any,
    invitation_token: str,
    participant_code: str,
    run_id: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Resolve invitation, consent and active Ultimatum stage/draft in one read."""
    marker = "%s" if DATABASE_URL else "?"
    token_hash = hashlib.sha256(invitation_token.encode("utf-8")).hexdigest()
    query = (
        "SELECT a.assignment_id,a.study_code,a.participant_id,a.condition_code,a.workflow_json,"
        "sp.participation_id,p.participant_code,"
        "EXISTS(SELECT 1 FROM experiment_consents AS consent "
        "WHERE consent.assignment_id=a.assignment_id AND consent.consent_version=" + marker
        + ") AS consent_accepted,"
        "progress.status AS active_stage_status,"
        "progress.current_run_id AS active_run_id,"
        "progress.draft_payload AS active_draft_payload,"
        "instrument.instrument_code AS active_instrument_code "
        "FROM experiment_assignments AS a "
        "JOIN participants AS p ON p.participant_id=a.participant_id "
        "JOIN study_participations AS sp ON sp.participant_id=a.participant_id "
        "AND sp.study_code=a.study_code "
        "LEFT JOIN experiment_stage_progress AS progress "
        "ON progress.participation_id=sp.participation_id AND progress.stage_code='ultimatum' "
        "LEFT JOIN instrument_runs AS active_run ON active_run.run_id=progress.current_run_id "
        "LEFT JOIN instrument_versions AS instrument "
        "ON instrument.instrument_version_id=active_run.instrument_version_id "
        "WHERE p.participant_code=" + marker
        + " AND a.invitation_token_hash=" + marker + " AND a.active=1"
    )
    row = connection.execute(
        query,
        (
            CONSENT_VERSION,
            normalize_participant_code(participant_code),
            token_hash,
        ),
    ).fetchone()
    if not row:
        raise HTTPException(403, "کد شرکت‌کننده یا لینک اختصاصی معتبر نیست.")
    if not bool(row["consent_accepted"]):
        raise HTTPException(403, "رضایت برای مشارکت ثبت نشده است.")
    if row["active_stage_status"] != "in_progress" or row["active_run_id"] != run_id:
        raise HTTPException(409, "این اجرای اولتیماتوم دیگر فعال نیست.")
    if row["active_instrument_code"] != "ultimatum":
        raise HTTPException(409, "شناسهٔ اجرای اولتیماتوم معتبر نیست.")

    joined = dict(row)
    assignment = {
        key: joined[key]
        for key in (
            "assignment_id",
            "study_code",
            "participant_id",
            "condition_code",
            "workflow_json",
            "participation_id",
            "participant_code",
        )
    }
    stage_state = {
        "status": joined["active_stage_status"],
        "current_run_id": joined["active_run_id"],
        "draft_payload": joined["active_draft_payload"],
        "instrument_code": joined["active_instrument_code"],
    }
    return assignment, stage_state



def _lookup_active_ultimatum_round(
    connection: Any,
    invitation_token: str,
    participant_code: str,
    run_id: str,
    round_id: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Validate invitation, consent, active run and round in one database round-trip."""
    marker = "%s" if DATABASE_URL else "?"
    token_hash = hashlib.sha256(invitation_token.encode("utf-8")).hexdigest()
    query = (
        "SELECT a.assignment_id,a.study_code,a.participant_id,a.condition_code,a.workflow_json,"
        "sp.participation_id,p.participant_code,"
        "EXISTS(SELECT 1 FROM experiment_consents AS consent "
        "WHERE consent.assignment_id=a.assignment_id AND consent.consent_version=" + marker
        + ") AS consent_accepted,"
        "progress.status AS active_stage_status,"
        "progress.current_run_id AS active_run_id,"
        "instrument.instrument_code AS active_instrument_code,"
        "trial.* "
        "FROM experiment_assignments AS a "
        "JOIN participants AS p ON p.participant_id=a.participant_id "
        "JOIN study_participations AS sp ON sp.participant_id=a.participant_id "
        "AND sp.study_code=a.study_code "
        "LEFT JOIN experiment_stage_progress AS progress "
        "ON progress.participation_id=sp.participation_id AND progress.stage_code='ultimatum' "
        "LEFT JOIN instrument_runs AS active_run ON active_run.run_id=progress.current_run_id "
        "LEFT JOIN instrument_versions AS instrument "
        "ON instrument.instrument_version_id=active_run.instrument_version_id "
        "LEFT JOIN ultimatum_trials AS trial ON trial.round_id=" + marker
        + " AND trial.run_id=" + marker
        + " WHERE p.participant_code=" + marker
        + " AND a.invitation_token_hash=" + marker + " AND a.active=1"
    )
    row = connection.execute(
        query,
        (
            CONSENT_VERSION,
            round_id,
            run_id,
            normalize_participant_code(participant_code),
            token_hash,
        ),
    ).fetchone()
    if not row:
        raise HTTPException(403, "کد شرکت‌کننده یا لینک اختصاصی معتبر نیست.")
    if not bool(row["consent_accepted"]):
        raise HTTPException(403, "رضایت برای مشارکت ثبت نشده است.")
    if row["active_stage_status"] != "in_progress" or row["active_run_id"] != run_id:
        raise HTTPException(409, "این اجرای اولتیماتوم دیگر فعال نیست.")
    if row["active_instrument_code"] != "ultimatum":
        raise HTTPException(409, "شناسهٔ اجرای اولتیماتوم معتبر نیست.")
    if row["round_id"] is None:
        raise HTTPException(404, "دور اولتیماتوم پیدا نشد.")

    joined = dict(row)
    assignment = {
        key: joined[key]
        for key in (
            "assignment_id",
            "study_code",
            "participant_id",
            "condition_code",
            "workflow_json",
            "participation_id",
            "participant_code",
        )
    }
    trial_columns = (
        "round_id",
        "run_id",
        "phase",
        "block_code",
        "block_order",
        "trial_in_block",
        "trial_sequence",
        "role",
        "opponent_condition",
        "opponent_actual_type",
        "status",
        "participant_share",
        "opponent_share",
        "participant_decision",
        "opponent_decision",
        "accepted",
        "points_earned",
        "potential_points_lost",
        "reaction_time_ms",
        "wait_duration_ms",
        "started_at",
        "decision_screen_shown_at",
        "responded_at",
        "received_at",
        "completed_at",
        "payload",
    )
    record = {key: joined[key] for key in trial_columns}
    return assignment, record


def _validate_event_timestamps(events: dict[str, str], allowed: frozenset[str]) -> None:
    unknown = set(events) - allowed
    if unknown:
        raise HTTPException(422, "نام یکی از رخدادهای زمانی اولتیماتوم مجاز نیست.")
    for event_name, value in events.items():
        if not value or len(value) > 80:
            raise HTTPException(422, "زمان یکی از رخدادهای اولتیماتوم معتبر نیست.")
        try:
            datetime.fromisoformat(value.replace("Z", "+00:00"))
        except (TypeError, ValueError) as exc:
            raise HTTPException(422, "زمان رخدادهای اولتیماتوم باید ISO 8601 باشد.") from exc


def _sample_ai_offer_to_participant() -> tuple[int, dict[str, Any]]:
    # A discretized normal distribution on integers 0..10, centered at 5 points.
    values = list(range(ULTIMATUM_TOTAL_POINTS + 1))
    weights = [
        math.exp(-0.5 * ((value - ULTIMATUM_AI_OFFER_NORMAL_MEAN) / ULTIMATUM_AI_OFFER_NORMAL_SD) ** 2)
        for value in values
    ]
    ai_self_share = random.choices(values, weights=weights, k=1)[0]
    participant_share = ULTIMATUM_TOTAL_POINTS - ai_self_share
    return participant_share, {
        "model": "discrete_truncated_normal",
        "mean": ULTIMATUM_AI_OFFER_NORMAL_MEAN,
        "sd": ULTIMATUM_AI_OFFER_NORMAL_SD,
        "support": values,
        "weights": weights,
        "sampled_ai_self_share": ai_self_share,
        "sampled_participant_share": participant_share,
    }


def _sample_ai_response(ai_share: int) -> dict[str, Any]:
    # Provisional binomial-response operationalization: p is the share offered to the AI / 10.
    n = ULTIMATUM_BINOMIAL_N
    p = max(0.0, min(1.0, ai_share / ULTIMATUM_TOTAL_POINTS))
    draw = sum(1 for _ in range(n) if random.random() < p)
    accepted = draw >= ULTIMATUM_BINOMIAL_ACCEPT_THRESHOLD
    return {
        "model": "binomial_threshold",
        "n": n,
        "p": p,
        "draw": draw,
        "accept_threshold": ULTIMATUM_BINOMIAL_ACCEPT_THRESHOLD,
        "decision": "accept" if accepted else "reject",
    }


@app.post("/api/experiment/ultimatum/prepare")
def prepare_ultimatum_round(data: UltimatumPrepareIn):
    """Create a server-audited round; AI-generated offers are sampled on the server."""
    if data.phase == "practice":
        expected = ULTIMATUM_PRACTICE_BLOCKS.get(data.block_code)
        if data.block_order != 0 or data.trial_in_block != 1 or data.trial_sequence != 0 or not expected:
            raise HTTPException(422, "مشخصات دور تمرینی اولتیماتوم معتبر نیست.")
    else:
        expected = ULTIMATUM_BLOCKS.get(data.block_code)
        if (
            not expected
            or data.block_order not in {1, 2, 3, 4}
            or data.trial_sequence != (data.block_order - 1) * ULTIMATUM_MAIN_BLOCK_TRIALS + data.trial_in_block
        ):
            raise HTTPException(422, "مشخصات بلوک یا شمارهٔ دور اولتیماتوم معتبر نیست.")
    with conn() as connection:
        assignment, stage_state = _lookup_active_ultimatum_stage(
            connection, data.invitation_token, data.participant_code, data.run_id
        )
        marker = "%s" if DATABASE_URL else "?"
        run_draft = _json_value(stage_state["draft_payload"])
        server_block_order = run_draft.get("block_order")

        # Keep only the small fields needed for workflow validation. The JSONB payload
        # may contain client metadata and model audit data and is fetched only for a
        # specific idempotent retry, not for every subsequent round.
        practice_rows = connection.execute(
            "SELECT round_id,block_code,block_order,trial_in_block,status FROM ultimatum_trials "
            "WHERE run_id=" + marker + " AND phase='practice'",
            (data.run_id,),
        ).fetchall()
        if data.phase == "practice":
            if data.block_code == "practice_proposer_agent":
                if any(row["block_code"] != data.block_code for row in practice_rows) or any(
                    row["status"] == "completed" for row in practice_rows
                ):
                    raise HTTPException(409, "دور تمرینی پیشنهاددهنده قبلاً آغاز یا تکمیل شده است.")
            else:
                proposer = next(
                    (row for row in practice_rows if row["block_code"] == "practice_proposer_agent"),
                    None,
                )
                if not proposer or proposer["status"] != "completed" or any(
                    row["block_code"] not in {"practice_proposer_agent", "practice_responder_coworker"}
                    for row in practice_rows
                ):
                    raise HTTPException(409, "ابتدا باید دور تمرینی پیشنهاددهنده را کامل کنید.")
        else:
            if (
                not isinstance(server_block_order, list)
                or len(server_block_order) != ULTIMATUM_MAIN_BLOCK_COUNT
                or set(server_block_order) != set(ULTIMATUM_BLOCKS)
                or server_block_order[data.block_order - 1] != data.block_code
            ):
                raise HTTPException(409, "ترتیب بلوک با تخصیص ثبت‌شده در سرور مطابقت ندارد.")
            expected_practice = set(ULTIMATUM_PRACTICE_BLOCKS)
            if (
                len(practice_rows) != 2
                or {row["block_code"] for row in practice_rows} != expected_practice
                or any(row["status"] != "completed" for row in practice_rows)
            ):
                raise HTTPException(409, "برای شروع بازی اصلی باید هر دو دور تمرینی تکمیل شوند.")
            main_summary = connection.execute(
                "SELECT "
                "SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END) AS completed_count,"
                "MIN(CASE WHEN block_order=" + marker + " THEN block_code END) AS order_min_code,"
                "MAX(CASE WHEN block_order=" + marker + " THEN block_code END) AS order_max_code,"
                "MIN(CASE WHEN block_code=" + marker + " THEN block_order END) AS block_min_order,"
                "MAX(CASE WHEN block_code=" + marker + " THEN block_order END) AS block_max_order,"
                "MAX(CASE WHEN block_code=" + marker + " AND trial_in_block=" + marker
                + " THEN round_id END) AS existing_round_id,"
                "MAX(CASE WHEN block_code=" + marker + " AND trial_in_block=" + marker
                + " THEN status END) AS existing_status "
                "FROM ultimatum_trials WHERE run_id=" + marker + " AND phase='main'",
                (
                    data.block_order, data.block_order,
                    data.block_code, data.block_code,
                    data.block_code, data.trial_in_block,
                    data.block_code, data.trial_in_block,
                    data.run_id,
                ),
            ).fetchone()
            completed_main_count = int(main_summary["completed_count"] or 0)
            if data.trial_sequence != completed_main_count + 1:
                raise HTTPException(409, "دورهای اصلی باید به‌ترتیب و بدون پرش اجرا شوند.")

        if data.phase == "main":
            order_min_code = main_summary["order_min_code"]
            order_max_code = main_summary["order_max_code"]
            if order_min_code is not None and (
                order_min_code != data.block_code or order_max_code != data.block_code
            ):
                raise HTTPException(409, "ترتیب بلوک‌ها در این اجرای اولتیماتوم تغییر کرده است.")
            block_min_order = main_summary["block_min_order"]
            block_max_order = main_summary["block_max_order"]
            if block_min_order is not None and (
                int(block_min_order) != data.block_order or int(block_max_order) != data.block_order
            ):
                raise HTTPException(409, "هر بلوک اولتیماتوم باید فقط یک‌بار اجرا شود.")

        marker = "%s" if DATABASE_URL else "?"
        if data.phase == "main":
            existing_round_id = main_summary["existing_round_id"]
            existing = (
                {"round_id": existing_round_id, "status": main_summary["existing_status"]}
                if existing_round_id else None
            )
        else:
            existing = next(
                (
                    row for row in practice_rows
                    if row["block_code"] == data.block_code
                    and int(row["trial_in_block"]) == data.trial_in_block
                ),
                None,
            )
        if existing:
            if existing["status"] != "prepared":
                raise HTTPException(409, "این دور اولتیماتوم قبلاً تکمیل شده است.")
            full_existing = connection.execute(
                "SELECT * FROM ultimatum_trials WHERE round_id=" + marker + " AND run_id=" + marker,
                (existing["round_id"], data.run_id),
            ).fetchone()
            if not full_existing:
                raise HTTPException(409, "وضعیت این دور هم‌زمان تغییر کرده است؛ دوباره تلاش کنید.")
            row = dict(full_existing)
            return {
                "round_id": row["round_id"],
                "phase": row["phase"],
                "block_code": row["block_code"],
                "role": row["role"],
                "opponent_condition": row["opponent_condition"],
                "participant_share": row["participant_share"],
                "opponent_share": row["opponent_share"],
                "started_at": row["started_at"],
                "offer_model": _json_value(row["payload"]).get("offer_model"),
            }
        round_id = str(uuid.uuid4())
        timestamp = now()
        participant_share = None
        opponent_share = None
        payload: dict[str, Any] = {
            "phase": data.phase,
            "block_code": data.block_code,
            "block_order": data.block_order,
            "trial_in_block": data.trial_in_block,
            "trial_sequence": data.trial_sequence,
            "role": expected["role"],
            "opponent_condition": expected["opponent_condition"],
            "opponent_actual_type": "computer",
            "client_metadata": data.client_metadata,
        }
        if expected["role"] == "responder":
            participant_share, offer_model = _sample_ai_offer_to_participant()
            opponent_share = ULTIMATUM_TOTAL_POINTS - participant_share
            payload["offer_model"] = offer_model
            payload["opponent_offer_generated_at"] = now()
        connection.execute("SAVEPOINT ultimatum_prepare_insert")
        try:
            connection.execute(
                "INSERT INTO ultimatum_trials(round_id,run_id,phase,block_code,block_order,trial_in_block,"
                "trial_sequence,role,opponent_condition,opponent_actual_type,status,participant_share,opponent_share,"
                "started_at,received_at,payload) VALUES("
                + ",".join([marker] * 16) + ")",
                (
                    round_id, data.run_id, data.phase, data.block_code, data.block_order, data.trial_in_block,
                    data.trial_sequence, expected["role"], expected["opponent_condition"], "computer", "prepared",
                    participant_share, opponent_share, timestamp, now(), _json_param(payload),
                ),
            )
            connection.execute("RELEASE SAVEPOINT ultimatum_prepare_insert")
        except Exception as exc:
            connection.execute("ROLLBACK TO SAVEPOINT ultimatum_prepare_insert")
            connection.execute("RELEASE SAVEPOINT ultimatum_prepare_insert")
            concurrent = connection.execute(
                "SELECT * FROM ultimatum_trials WHERE run_id=" + marker
                + " AND phase=" + marker + " AND block_code=" + marker + " AND trial_in_block=" + marker,
                (data.run_id, data.phase, data.block_code, data.trial_in_block),
            ).fetchone()
            if concurrent and concurrent["status"] == "prepared":
                row = dict(concurrent)
                return {
                    "round_id": row["round_id"], "phase": row["phase"], "block_code": row["block_code"],
                    "role": row["role"], "opponent_condition": row["opponent_condition"],
                    "participant_share": row["participant_share"], "opponent_share": row["opponent_share"],
                    "started_at": row["started_at"], "offer_model": _json_value(row["payload"]).get("offer_model"),
                }
            raise HTTPException(503, "ثبت این دور موقتاً موفق نشد؛ لطفاً دوباره تلاش کنید.") from exc
        return {
            "round_id": round_id,
            "phase": data.phase,
            "block_code": data.block_code,
            "role": expected["role"],
            "opponent_condition": expected["opponent_condition"],
            "participant_share": participant_share,
            "opponent_share": opponent_share,
            "started_at": timestamp,
            "offer_model": payload.get("offer_model"),
        }


def _ultimatum_score_totals(connection: Any, run_id: str, block_code: str) -> tuple[dict[str, int], dict[str, int]]:
    """Read run and per-block scores in one bounded aggregate query."""
    marker = "%s" if DATABASE_URL else "?"
    rows = connection.execute(
        "SELECT block_code,COALESCE(SUM(points_earned),0) AS score,"
        "COALESCE(SUM(potential_points_lost),0) AS lost "
        "FROM ultimatum_trials WHERE run_id=" + marker
        + " AND phase='main' AND status='completed' GROUP BY block_code",
        (run_id,),
    ).fetchall()
    total_score = 0
    total_lost = 0
    selected_block = {"score": 0, "lost": 0}
    for row in rows:
        score = int(row["score"] or 0)
        lost = int(row["lost"] or 0)
        total_score += score
        total_lost += lost
        if row["block_code"] == block_code:
            selected_block = {"score": score, "lost": lost}
    return {"score": total_score, "lost": total_lost}, selected_block


def _resolve_ultimatum_round(data: UltimatumResolveIn):
    """Resolve participant choice, generate AI responses server-side, and persist trial-level data."""
    _validate_event_timestamps(data.client_event_timestamps, ULTIMATUM_PRE_RESOLVE_EVENT_FIELDS)
    with conn() as connection:
        assignment, record = _lookup_active_ultimatum_round(
            connection,
            data.invitation_token,
            data.participant_code,
            data.run_id,
            data.round_id,
        )
        marker = "%s" if DATABASE_URL else "?"

        def stored_resolve_result(completed_record: dict[str, Any]) -> dict[str, Any]:
            saved_payload = _json_value(completed_record["payload"])
            existing_result = saved_payload.get("resolve_result")
            if isinstance(existing_result, dict) and existing_result.get("round_id"):
                return existing_result
            total, block_total = _ultimatum_score_totals(
                connection, data.run_id, completed_record["block_code"]
            )
            recovered = {
                "round_id": completed_record["round_id"],
                "phase": completed_record["phase"],
                "block_code": completed_record["block_code"],
                "block_order": completed_record["block_order"],
                "trial_in_block": completed_record["trial_in_block"],
                "role": completed_record["role"],
                "opponent_condition": completed_record["opponent_condition"],
                "participant_share": completed_record["participant_share"],
                "opponent_share": completed_record["opponent_share"],
                "participant_decision": completed_record["participant_decision"],
                "opponent_decision": completed_record["opponent_decision"],
                "accepted": bool(completed_record["accepted"]),
                "points_earned": int(completed_record["points_earned"] or 0),
                "potential_points_lost": int(completed_record["potential_points_lost"] or 0),
                "total_score": int(total["score"] or 0),
                "total_potential_lost": int(total["lost"] or 0),
                "block_score": int(block_total["score"] or 0),
                "block_potential_lost": int(block_total["lost"] or 0),
                "resolved_at": completed_record["completed_at"],
            }
            saved_payload["resolve_result"] = recovered
            connection.execute(
                "UPDATE ultimatum_trials SET payload=" + marker + " WHERE round_id=" + marker + " AND run_id=" + marker,
                (_json_param(saved_payload), completed_record["round_id"], data.run_id),
            )
            return recovered

        if record["status"] == "completed":
            return stored_resolve_result(record)
        if record["role"] == "proposer":
            if data.participant_offer_to_opponent is None or data.participant_decision is not None:
                raise HTTPException(422, "برای نقش پیشنهاددهنده باید مقدار پیشنهاد ارسال شود.")
            opponent_share = int(data.participant_offer_to_opponent)
            participant_share = ULTIMATUM_TOTAL_POINTS - opponent_share
            ai_response = _sample_ai_response(opponent_share)
            opponent_decision = ai_response["decision"]
            accepted = opponent_decision == "accept"
            participant_decision = None
            payload = _json_value(record["payload"])
            payload["decision_model"] = ai_response
            payload["opponent_response_generated_at"] = now()
            payload["participant_offer_to_opponent"] = opponent_share
            payload["participant_offer_to_self"] = participant_share
        else:
            if data.participant_decision is None or data.participant_offer_to_opponent is not None:
                raise HTTPException(422, "برای نقش پاسخ‌دهنده باید قبول یا رد ثبت شود.")
            participant_share = int(record["participant_share"])
            opponent_share = int(record["opponent_share"])
            participant_decision = data.participant_decision
            opponent_decision = None
            accepted = participant_decision == "accept"
            payload = _json_value(record["payload"])
            payload["computer_offer_to_participant"] = participant_share
            payload["computer_offer_to_self"] = opponent_share
        points_earned = participant_share if accepted else 0
        potential_points_lost = 0 if accepted else participant_share
        completed_at = now()
        responded_at = data.responded_at or completed_at
        received_at = now()
        reaction_time = data.reaction_time_ms
        wait_duration = data.wait_duration_ms
        payload.update({
            "participant_share": participant_share,
            "opponent_share": opponent_share,
            "participant_decision": participant_decision,
            "opponent_decision": opponent_decision,
            "accepted": accepted,
            "points_earned": points_earned,
            "potential_points_lost": potential_points_lost,
            "reaction_time_ms": reaction_time,
            "wait_duration_ms": wait_duration,
            "planned_wait_duration_ms": data.planned_wait_duration_ms,
            "decision_screen_shown_at": data.decision_screen_shown_at,
            "responded_at": responded_at,
            "client_metadata_resolve": data.client_metadata,
            "server_resolved_at": completed_at,
        })
        existing_event_timestamps = payload.get("client_event_timestamps") or {}
        existing_event_timestamps.update(data.client_event_timestamps)
        payload["client_event_timestamps"] = existing_event_timestamps
        update_cursor = connection.execute(
            "UPDATE ultimatum_trials SET status='completed',participant_share=" + marker
            + ",opponent_share=" + marker + ",participant_decision=" + marker + ",opponent_decision=" + marker
            + ",accepted=" + marker + ",points_earned=" + marker + ",potential_points_lost=" + marker
            + ",reaction_time_ms=" + marker + ",wait_duration_ms=" + marker + ",decision_screen_shown_at=" + marker
            + ",responded_at=" + marker + ",received_at=" + marker + ",completed_at=" + marker + ",payload=" + marker
            + " WHERE round_id=" + marker + " AND status='prepared'",
            (
                participant_share, opponent_share, participant_decision, opponent_decision, int(accepted),
                points_earned, potential_points_lost, reaction_time, wait_duration, data.decision_screen_shown_at,
                responded_at, received_at, completed_at, _json_param(payload), data.round_id,
            ),
        )
        if update_cursor.rowcount != 1:
            # A concurrent duplicate submission may have completed this round first.
            # Return the persisted result without double-counting the run.
            current = connection.execute(
                "SELECT * FROM ultimatum_trials WHERE round_id=" + marker + " AND run_id=" + marker,
                (data.round_id, data.run_id),
            ).fetchone()
            if current and current["status"] == "completed":
                return stored_resolve_result(dict(current))
            raise HTTPException(409, "این دور هم‌زمان ثبت شده است؛ وضعیت بازی را دوباره بررسی کنید.")
        connection.execute(
            "UPDATE instrument_runs SET trial_count=trial_count+1 WHERE run_id=" + marker,
            (data.run_id,),
        )
        total, block_total = _ultimatum_score_totals(
            connection, data.run_id, record["block_code"]
        )
        result = {
            "round_id": data.round_id,
            "phase": record["phase"],
            "block_code": record["block_code"],
            "block_order": record["block_order"],
            "trial_in_block": record["trial_in_block"],
            "role": record["role"],
            "opponent_condition": record["opponent_condition"],
            "participant_share": participant_share,
            "opponent_share": opponent_share,
            "participant_decision": participant_decision,
            "opponent_decision": opponent_decision,
            "accepted": accepted,
            "points_earned": points_earned,
            "potential_points_lost": potential_points_lost,
            "total_score": int(total["score"] or 0),
            "total_potential_lost": int(total["lost"] or 0),
            "block_score": int(block_total["score"] or 0),
            "block_potential_lost": int(block_total["lost"] or 0),
            "resolved_at": completed_at,
        }
        payload["resolve_result"] = result
        connection.execute(
            "UPDATE ultimatum_trials SET payload=" + marker + " WHERE round_id=" + marker,
            (_json_param(payload), data.round_id),
        )
        return result


def _next_ultimatum_request(data: UltimatumResolveIn, result: dict[str, Any]) -> dict[str, Any] | None:
    if result["phase"] == "practice" and result["block_code"] == "practice_proposer_agent":
        return {"phase": "practice", "block_code": "practice_responder_coworker", "block_order": 0,
                "trial_in_block": 1, "trial_sequence": 0}
    if result["phase"] == "main" and result["trial_in_block"] < ULTIMATUM_MAIN_BLOCK_TRIALS:
        trial = result["trial_in_block"] + 1
        order = result["block_order"]
        return {"phase": "main", "block_code": result["block_code"], "block_order": order,
                "trial_in_block": trial, "trial_sequence": (order - 1) * 5 + trial}
    if result["phase"] == "main" and result["block_order"] == ULTIMATUM_MAIN_BLOCK_COUNT:
        return None
    with conn() as connection:
        _assignment, state = _lookup_active_ultimatum_stage(
            connection, data.invitation_token, data.participant_code, data.run_id
        )
        order_list = _json_value(state["draft_payload"])["block_order"]
    order = 1 if result["phase"] == "practice" else result["block_order"] + 1
    return {"phase": "main", "block_code": order_list[order - 1], "block_order": order,
            "trial_in_block": 1, "trial_sequence": (order - 1) * 5 + 1}


@app.post("/api/experiment/ultimatum/resolve")
def resolve_ultimatum_round(data: UltimatumResolveIn):
    result = _resolve_ultimatum_round(data)
    if data.auxiliary_events:
        result = dict(result)
        result["acknowledged_event_round_ids"] = _append_auxiliary_events(data, data.auxiliary_events)
    if not data.prepare_next:
        return result
    # The choice is committed before optional prefetch. A prefetch failure must not
    # turn a successfully persisted response into a failed resolve.
    result = dict(result)
    result["next_round"] = None
    result["next_round_request"] = None
    try:
        request = _next_ultimatum_request(data, result)
        if request:
            result["next_round"] = prepare_ultimatum_round(UltimatumPrepareIn(
                invitation_token=data.invitation_token, participant_code=data.participant_code,
                run_id=data.run_id, client_metadata=data.client_metadata, **request
            ))
            result["next_round_request"] = request
    except HTTPException as exc:
        if exc.status_code not in {409, 503}:
            raise
        result["prefetch_deferred"] = True
    except Exception as exc:
        logging.getLogger(__name__).warning("Round prefetch deferred: %s", type(exc).__name__)
        result["prefetch_deferred"] = True
    return result


def _record_round_events(connection: Any, assignment: dict[str, Any], run_id: str,
                         item: UltimatumEventItem, extended: bool = False) -> None:
    marker = "%s" if DATABASE_URL else "?"
    row = connection.execute(
        "SELECT trial.status,trial.payload FROM ultimatum_trials AS trial "
        "JOIN instrument_runs AS run ON run.run_id=trial.run_id "
        "JOIN instrument_versions AS instrument "
        "ON instrument.instrument_version_id=run.instrument_version_id "
        "WHERE trial.round_id=" + marker + " AND trial.run_id=" + marker
        + " AND run.participation_id=" + marker + " AND instrument.instrument_code='ultimatum'"
        + (" FOR UPDATE OF trial" if DATABASE_URL else ""),
        (item.round_id, run_id, assignment["participation_id"]),
    ).fetchone()
    if not row:
        raise HTTPException(404, "دور اولتیماتوم برای این شرکت‌کننده پیدا نشد.")
    if row["status"] != "completed":
        raise HTTPException(409, "رخدادهای پس از نتیجه فقط برای دور تکمیل‌شده ثبت می‌شوند.")
    payload = _json_value(row["payload"])
    timestamps = payload.get("client_event_timestamps") or {}
    first_wait_end = "waiting_ended_at" not in timestamps
    for event_name, event_time in item.event_timestamps.items():
        timestamps.setdefault(event_name, event_time)
    payload["client_event_timestamps"] = timestamps
    payload.setdefault("client_event_server_received_at", now())
    update_wait = (extended and first_wait_end and "waiting_ended_at" in item.event_timestamps
                   and item.wait_duration_ms is not None and payload.get("wait_duration_ms") is None)
    if update_wait:
        payload["wait_duration_ms"] = item.wait_duration_ms
    connection.execute(
        "UPDATE ultimatum_trials SET payload=" + marker
        + (",wait_duration_ms=" + marker if update_wait else "")
        + " WHERE round_id=" + marker + " AND run_id=" + marker,
        ((_json_param(payload), item.wait_duration_ms, item.round_id, run_id) if update_wait
         else (_json_param(payload), item.round_id, run_id)),
    )


@app.post("/api/experiment/ultimatum/events")
def record_ultimatum_events(data: UltimatumEventsIn):
    """Compatibility endpoint; preserve first-write timestamps, including old attempts."""
    _validate_event_timestamps(data.event_timestamps, ULTIMATUM_POST_RESOLVE_EVENT_FIELDS)
    with conn() as connection:
        if not DATABASE_URL:
            connection.execute("BEGIN IMMEDIATE")
        assignment = _lookup_assignment(connection, data.invitation_token, data.participant_code, require_consent=True)
        item = UltimatumEventItem(round_id=data.round_id, event_timestamps=data.event_timestamps)
        _record_round_events(connection, assignment, data.run_id, item)
        marker = "%s" if DATABASE_URL else "?"
        saved = connection.execute("SELECT payload FROM ultimatum_trials WHERE round_id=" + marker,
                                   (data.round_id,)).fetchone()
        timestamps = _json_value(saved["payload"])["client_event_timestamps"]
    return {"round_id": data.round_id, "saved": True, "event_timestamps": timestamps}


@app.post("/api/experiment/ultimatum/events/batch")
def record_ultimatum_event_batch(data: UltimatumEventsBatchIn):
    """One bounded atomic upload for non-critical events; no changed random outcomes."""
    allowed = ULTIMATUM_POST_RESOLVE_EVENT_FIELDS | {"waiting_ended_at"}
    if len({item.round_id for item in data.events}) != len(data.events):
        raise HTTPException(422, "هر دور فقط یک‌بار در بستهٔ زمان‌ها مجاز است.")
    for item in data.events:
        _validate_event_timestamps(item.event_timestamps, allowed)
        if item.wait_duration_ms is not None and "waiting_ended_at" not in item.event_timestamps:
            raise HTTPException(422, "مدت انتظار باید همراه زمان پایان انتظار باشد.")
    with conn() as connection:
        if not DATABASE_URL:
            connection.execute("BEGIN IMMEDIATE")
        assignment = _lookup_assignment(connection, data.invitation_token, data.participant_code, require_consent=True)
        # Stable lock order avoids deadlocks between overlapping batches.
        for item in sorted(data.events, key=lambda item: item.round_id):
            _record_round_events(connection, assignment, data.run_id, item, extended=True)
    return {"saved": True, "round_ids": [item.round_id for item in data.events]}


def _complete_experiment_stage(data: StageCompleteIn):
    """Mark a module complete only for its currently active attempt."""
    with conn() as connection:
        assignment = _lookup_assignment(
            connection, data.invitation_token, data.participant_code, require_consent=True
        )
        marker = "%s" if DATABASE_URL else "?"
        if data.step_id:
            state = connection.execute(
                "SELECT status,current_run_id,attempt_number,draft_payload FROM experiment_workflow_progress "
                "WHERE assignment_id=" + marker + " AND step_instance_id=" + marker,
                (assignment["assignment_id"], data.step_id),
            ).fetchone()
        else:
            state = connection.execute(
                "SELECT status,current_run_id,attempt_number,draft_payload FROM experiment_stage_progress "
                "WHERE participation_id=" + marker + " AND stage_code=" + marker,
                (assignment["participation_id"], data.stage_code),
            ).fetchone()
        if not state:
            raise HTTPException(409, "این مرحله هنوز شروع نشده است.")
        if state["current_run_id"] != data.run_id:
            raise HTTPException(409, "این اجرای مرحله دیگر فعال نیست.")
        run = connection.execute(
            "SELECT participation_id,metadata FROM instrument_runs WHERE run_id=" + marker
            + (" FOR UPDATE" if DATABASE_URL else ""), (data.run_id,),
        ).fetchone()
        metadata = _json_value(run["metadata"]) if run else {}
        if (not run or run["participation_id"] != assignment["participation_id"]
                or metadata.get("stage_code") != data.stage_code
                or metadata.get("workflow_step_id") != data.step_id):
            raise HTTPException(422, "نوع ماژول یا شناسهٔ مرحله با اجرای ثبت‌شده یکسان نیست.")
        if metadata.get("workflow_module_code") == "dd_standalone":
            _validate_dd_responses(connection, data.run_id, 5, standalone=True)
        if state["status"] == "completed":
            return {"completed": True, "already_completed": True, "stage_code": data.stage_code, "step_id": data.step_id}
        if data.stage_code == "intermission":
            if not data.step_id or not (data.final_payload or {}).get("continue_clicked_at"):
                raise HTTPException(422, "ادامهٔ استراحت باید در همان مرحله تأیید شود.")
            metadata["intermission_completion"] = data.final_payload
            connection.execute("UPDATE instrument_runs SET metadata=" + marker + " WHERE run_id=" + marker,
                               (_json_param(metadata), data.run_id))
        timestamp = now()
        if data.stage_code == "questionnaire":
            if data.final_payload is None:
                raise HTTPException(422, "پاسخ نهایی پرسشنامه باید ارسال شود.")
            final_answers = data.final_payload.get("answers")
            if not isinstance(final_answers, dict) or any(
                code not in final_answers
                or str(final_answers[code]) not in {"1", "2", "3", "4", "5", "NA"}
                for code in QUESTIONNAIRE_QUESTION_CODES
            ):
                raise HTTPException(422, "تمام سؤال‌های پرسشنامه باید پاسخ معتبر داشته باشند.")
            count_row = connection.execute(
                "SELECT COUNT(*) AS n FROM questionnaire_responses WHERE run_id=" + marker,
                (data.run_id,),
            ).fetchone()
            if int(count_row["n"]) != len(QUESTIONNAIRE_QUESTION_CODES):
                raise HTTPException(422, "پاسخ همهٔ سؤال‌ها هنوز در سرور ذخیره نشده است.")
            payload_param = _json_param(data.final_payload)
            connection.execute(
                "UPDATE experiment_stage_progress SET status='completed',draft_payload=" + marker
                + ",completed_at=" + marker + ",updated_at=" + marker
                + " WHERE participation_id=" + marker + " AND stage_code=" + marker,
                (payload_param, timestamp, timestamp, assignment["participation_id"], data.stage_code),
            )
        elif data.stage_code == "ultimatum":
            if data.final_payload is None:
                raise HTTPException(422, "خلاصهٔ نهایی اولتیماتوم باید ارسال شود.")
            round_rows = connection.execute(
                "SELECT phase,block_code,block_order,trial_in_block,trial_sequence,role,opponent_condition,status "
                "FROM ultimatum_trials WHERE run_id=" + marker + " ORDER BY phase,block_order,trial_sequence",
                (data.run_id,),
            ).fetchall()
            practice = [dict(row) for row in round_rows if row["phase"] == "practice"]
            main = [dict(row) for row in round_rows if row["phase"] == "main"]
            expected_practice = {
                "practice_proposer_agent": ("proposer", "agent"),
                "practice_responder_coworker": ("responder", "coworker"),
            }
            if len(practice) != 2 or any(
                row["status"] != "completed"
                or row["trial_in_block"] != 1
                or row["block_code"] not in expected_practice
                or (row["role"], row["opponent_condition"]) != expected_practice[row["block_code"]]
                for row in practice
            ) or {row["block_code"] for row in practice} != set(expected_practice):
                raise HTTPException(422, "دو دور تمرینی اولتیماتوم هنوز کامل ثبت نشده‌اند.")
            if len(main) != ULTIMATUM_MAIN_TRIALS:
                raise HTTPException(422, "همهٔ ۲۰ دور اصلی اولتیماتوم هنوز ثبت نشده‌اند.")
            if {row["block_order"] for row in main} != {1, 2, 3, 4}:
                raise HTTPException(422, "ترتیب چهار بلوک اولتیماتوم کامل نیست.")
            for block_code, expected in ULTIMATUM_BLOCKS.items():
                block_rows = [row for row in main if row["block_code"] == block_code]
                if (
                    len(block_rows) != ULTIMATUM_MAIN_BLOCK_TRIALS
                    or {row["trial_in_block"] for row in block_rows} != {1, 2, 3, 4, 5}
                    or len({row["block_order"] for row in block_rows}) != 1
                    or any(row["status"] != "completed" for row in block_rows)
                    or any((row["role"], row["opponent_condition"]) != (expected["role"], expected["opponent_condition"]) for row in block_rows)
                    or any(row["trial_sequence"] != (row["block_order"] - 1) * 5 + row["trial_in_block"] for row in block_rows)
                ):
                    raise HTTPException(422, "یکی از بلوک‌های اولتیماتوم ناقص یا نامعتبر است.")
            order_by_position = {
                int(row["block_order"]): row["block_code"]
                for row in main
            }
            if len(order_by_position) != 4 or set(order_by_position) != {1, 2, 3, 4}:
                raise HTTPException(422, "هر موقعیت بلوک باید دقیقاً به یک بلوک اختصاص داشته باشد.")
            recorded_order = data.final_payload.get("block_order")
            if (
                not isinstance(recorded_order, list)
                or len(recorded_order) != 4
                or any(recorded_order[index] != order_by_position[index + 1] for index in range(4))
            ):
                raise HTTPException(422, "ترتیب بلوک‌های ثبت‌شده با داده‌های دورها مطابقت ندارد.")
            totals = connection.execute(
                "SELECT COALESCE(SUM(points_earned),0) AS score,"
                "COALESCE(SUM(potential_points_lost),0) AS lost "
                "FROM ultimatum_trials WHERE run_id=" + marker + " AND phase='main' AND status='completed'",
                (data.run_id,),
            ).fetchone()
            try:
                submitted_score = int(data.final_payload.get("final_score"))
                submitted_lost = int(data.final_payload.get("total_potential_points_lost"))
            except (TypeError, ValueError):
                raise HTTPException(422, "امتیاز نهایی یا امتیاز ازدست‌رفتهٔ نهایی معتبر نیست.")
            if submitted_score != int(totals["score"] or 0) or submitted_lost != int(totals["lost"] or 0):
                raise HTTPException(422, "جمع امتیازهای نهایی با پاسخ‌های ثبت‌شده مطابقت ندارد.")
            payload_param = _json_param(data.final_payload)
            connection.execute(
                "UPDATE experiment_stage_progress SET status='completed',draft_payload=" + marker
                + ",completed_at=" + marker + ",updated_at=" + marker
                + " WHERE participation_id=" + marker + " AND stage_code=" + marker,
                (payload_param, timestamp, timestamp, assignment["participation_id"], data.stage_code),
            )
        else:
            connection.execute(
                "UPDATE experiment_stage_progress SET status='completed',completed_at=" + marker
                + ",updated_at=" + marker
                + " WHERE participation_id=" + marker + " AND stage_code=" + marker,
                (timestamp, timestamp, assignment["participation_id"], data.stage_code),
            )
        if data.step_id:
            cursor = connection.execute(
                "UPDATE experiment_workflow_progress SET status='completed',completed_at=" + marker
                + ",updated_at=" + marker + " WHERE assignment_id=" + marker
                + " AND step_instance_id=" + marker + " AND current_run_id=" + marker,
                (timestamp, timestamp, assignment["assignment_id"], data.step_id, data.run_id),
            )
            if cursor.rowcount == 0:
                raise HTTPException(409, "این نوبت از ماژول دیگر فعال نیست.")
        connection.execute(
            "UPDATE instrument_runs SET status='completed',completed_at=" + marker + " WHERE run_id=" + marker,
            (timestamp, data.run_id),
        )
    return {"completed": True, "already_completed": False, "stage_code": data.stage_code, "step_id": data.step_id}



def _append_auxiliary_events(identity: Any, events: list[UltimatumEventItem]) -> list[str]:
    try:
        response = record_ultimatum_event_batch(UltimatumEventsBatchIn(
            invitation_token=identity.invitation_token, participant_code=identity.participant_code,
            run_id=identity.run_id, events=events,
        ))
        return response["round_ids"]
    except HTTPException:
        # A bad auxiliary item never invalidates an already committed choice.
        # No ack is returned; the local rows remain available for recovery.
        return []
    except Exception as exc:
        logging.getLogger(__name__).warning("Auxiliary timing upload deferred: %s", type(exc).__name__)
        return []


@app.post("/api/experiment/stages/complete")
def complete_experiment_stage(data: StageCompleteIn):
    result = _complete_experiment_stage(data)
    if data.timing_checkpoints:
        try:
            result["acknowledged_checkpoints"] = track_client_time(ClientTimingIn(
                invitation_token=data.invitation_token, participant_code=data.participant_code,
                checkpoints=data.timing_checkpoints))["acknowledged_checkpoints"]
        except Exception as exc:
            logging.getLogger(__name__).warning("Client timing deferred: %s", type(exc).__name__)
    if data.stage_code == "ultimatum" and data.auxiliary_events:
        result["acknowledged_event_round_ids"] = _append_auxiliary_events(data, data.auxiliary_events)
    return result


def _validate_dd_responses(connection, run_id, expected, *, standalone=False):
    marker = "%s" if DATABASE_URL else "?"
    rows = connection.execute(
        "SELECT task_code,magnitude,trial_in_block FROM lifetrade_trials WHERE run_id=" + marker,
        (run_id,),
    ).fetchall()
    if len(rows) != expected:
        raise HTTPException(422, f"همهٔ {expected} پاسخ بازی انتخاب هنوز ثبت نشده‌اند.")
    if standalone and (any(row["task_code"] != "KT" or row["magnitude"] != "100M" for row in rows)
                       or {row["trial_in_block"] for row in rows} != {1, 2, 3, 4, 5}):
        raise HTTPException(422, "پاسخ‌های DD مستقل باید دقیقاً پنج دور با مبلغ تعیین‌شده باشند.")


@app.post("/api/experiment/dd/complete")
def complete_dd_task(data: StageCompleteIn):
    if data.stage_code != "dd_lifetrade":
        raise HTTPException(422, "این مسیر فقط برای بازی انتخاب است.")
    marker = "%s" if DATABASE_URL else "?"
    with conn() as connection:
        assignment = _lookup_assignment(connection, data.invitation_token, data.participant_code, require_consent=True)
        run = connection.execute(
            "SELECT run.metadata FROM instrument_runs AS run "
            "JOIN instrument_versions AS version ON version.instrument_version_id=run.instrument_version_id "
            "WHERE run.run_id=" + marker + " AND run.participation_id=" + marker
            + " AND version.instrument_code='lifetrade'",
            (data.run_id, assignment["participation_id"]),
        ).fetchone()
        if not run:
            raise HTTPException(403, "این اجرا متعلق به بازی انتخاب همین دعوت نیست.")
        standalone = _json_value(run["metadata"]).get("workflow_module_code") == "dd_standalone"
        _validate_dd_responses(connection, data.run_id, 5 if standalone else 30, standalone=standalone)
    return complete_experiment_stage(data)


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _float_or_none(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _persist_trials(run_id: str, records: list[TrialRecord]) -> tuple[int, int]:
    """Insert one bounded LifeTrade batch atomically; never mix data with other instruments."""
    if not records:
        return 0, 0

    received_times = [now() for _ in records]
    values = []
    for record, received_at in zip(records, received_times):
        row = record.row
        payload = (
            record.client_event_id,
            run_id,
            received_at,
            str(row.get("task")) if row.get("task") is not None else None,
            _int_or_none(row.get("task_order")),
            str(row.get("block_name")) if row.get("block_name") is not None else None,
            str(row.get("magnitude")) if row.get("magnitude") is not None else None,
            _int_or_none(row.get("trial_in_block")),
            _int_or_none(row.get("index")),
            str(row.get("user_choice_type")) if row.get("user_choice_type") is not None else None,
            _float_or_none(row.get("reaction_time_ms")),
            row,
        )
        if DATABASE_URL:
            values.append(payload[:-1] + (Jsonb(payload[-1]),))
        else:
            values.append(
                payload[:-1]
                + (json.dumps(payload[-1], ensure_ascii=False, separators=(",", ":")),)
            )

    marker = "%s" if DATABASE_URL else "?"
    value_sql = ",".join(["(" + ",".join([marker] * 12) + ")"] * len(values))
    params = tuple(value for row in values for value in row)

    with conn() as connection:
        if not DATABASE_URL:
            connection.execute("BEGIN IMMEDIATE")

        if DATABASE_URL:
            exists = connection.execute(
                "SELECT ir.metadata FROM instrument_runs ir "
                "JOIN instrument_versions iv ON iv.instrument_version_id=ir.instrument_version_id "
                "JOIN instruments i ON i.instrument_code=iv.instrument_code "
                "WHERE ir.run_id=%s AND i.instrument_code=%s",
                (run_id, INSTRUMENT_CODE),
            ).fetchone()
            sql = (
                "INSERT INTO lifetrade_trials(client_event_id,run_id,received_at,task_code,task_order,"
                "block_name,magnitude,trial_in_block,stimulus_index,choice_type,response_time_ms,payload) VALUES "
                + value_sql
                + " ON CONFLICT(client_event_id) DO NOTHING"
            )
        else:
            exists = connection.execute(
                "SELECT ir.metadata FROM instrument_runs ir "
                "JOIN instrument_versions iv ON iv.instrument_version_id=ir.instrument_version_id "
                "JOIN instruments i ON i.instrument_code=iv.instrument_code "
                "WHERE ir.run_id=? AND i.instrument_code=?",
                (run_id, INSTRUMENT_CODE),
            ).fetchone()
            sql = (
                "INSERT OR IGNORE INTO lifetrade_trials(client_event_id,run_id,received_at,task_code,task_order,"
                "block_name,magnitude,trial_in_block,stimulus_index,choice_type,response_time_ms,payload) VALUES "
                + value_sql
            )
        if not exists:
            raise HTTPException(404, "Unknown LifeTrade run")

        if _json_value(exists["metadata"]).get("workflow_module_code") == "dd_standalone":
            for record in records:
                row = record.row
                if (row.get("task") != "KT" or row.get("magnitude") != "100M"
                        or row.get("trial_in_block") not in (1, 2, 3, 4, 5)):
                    raise HTTPException(422, "دادهٔ پاسخ با طراحی DD مستقل سازگار نیست.")

        cursor = connection.execute(sql, params)
        inserted = max(0, cursor.rowcount or 0)
        duplicates = len(records) - inserted
        if inserted:
            if DATABASE_URL:
                connection.execute(
                    "UPDATE instrument_runs SET trial_count=trial_count+%s WHERE run_id=%s",
                    (inserted, run_id),
                )
            else:
                connection.execute(
                    "UPDATE instrument_runs SET trial_count=trial_count+? WHERE run_id=?",
                    (inserted, run_id),
                )
    return inserted, duplicates


@app.post("/api/trial")
def save_trial(data: TrialIn):
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
                "UPDATE instrument_runs SET status=%s,completed_at=%s WHERE run_id=%s RETURNING run_id",
                (status, now(), data.session_id),
            ).fetchone()
        else:
            result = connection.execute(
                "UPDATE instrument_runs SET status=?,completed_at=? WHERE run_id=?",
                (status, now(), data.session_id),
            ).rowcount
    if not result:
        raise HTTPException(404, "Unknown LifeTrade run")
    return {"saved": True, "status": status}


def authorize(token: str | None) -> None:
    if not ADMIN_TOKEN or not secrets.compare_digest(token or "", ADMIN_TOKEN):
        raise HTTPException(401, "Unauthorized")


@app.get("/api/admin/maintenance/preview")
def maintenance_preview(
    x_admin_token: str | None = Header(default=None),
    scope: Literal["main", "test"] = "main",
):
    """Return exact counts before any scoped data reset."""
    authorize(x_admin_token)
    marker = "%s" if DATABASE_URL else "?"
    if scope == "test":
        condition = "substr(study_code,1,12)='barsan-test-'"
        params: tuple[Any, ...] = ()
    else:
        condition = "study_code=" + marker
        params = (STUDY_CODE,)

    queries = {
        "assignments": ("SELECT COUNT(*) AS n FROM experiment_assignments WHERE " + condition, params),
        "group_links": ("SELECT COUNT(*) AS n FROM experiment_group_links WHERE " + condition, params),
        "participations": ("SELECT COUNT(*) AS n FROM study_participations WHERE " + condition, params),
        "runs": (
            "SELECT COUNT(*) AS n FROM instrument_runs ir JOIN study_participations sp "
            "ON sp.participation_id=ir.participation_id WHERE " + condition.replace("study_code", "sp.study_code"),
            params,
        ),
        "questionnaire_responses": (
            "SELECT COUNT(*) AS n FROM questionnaire_responses qr JOIN instrument_runs ir ON ir.run_id=qr.run_id "
            "JOIN study_participations sp ON sp.participation_id=ir.participation_id WHERE "
            + condition.replace("study_code", "sp.study_code"), params,
        ),
        "lifetrade_trials": (
            "SELECT COUNT(*) AS n FROM lifetrade_trials t JOIN instrument_runs ir ON ir.run_id=t.run_id "
            "JOIN study_participations sp ON sp.participation_id=ir.participation_id WHERE "
            + condition.replace("study_code", "sp.study_code"), params,
        ),
        "ultimatum_trials": (
            "SELECT COUNT(*) AS n FROM ultimatum_trials u JOIN instrument_runs ir ON ir.run_id=u.run_id "
            "JOIN study_participations sp ON sp.participation_id=ir.participation_id WHERE "
            + condition.replace("study_code", "sp.study_code"), params,
        ),
        "participants": (
            "SELECT COUNT(DISTINCT p.participant_id) AS n FROM participants p JOIN experiment_assignments a "
            "ON a.participant_id=p.participant_id WHERE " + condition.replace("study_code", "a.study_code"), params,
        ),
    }
    counts = {}
    with conn() as connection:
        for name, (query, query_params) in queries.items():
            counts[name] = int(connection.execute(query, query_params).fetchone()["n"] or 0)
        condition_a = condition.replace("study_code", "a.study_code")
        assignments = [dict(row) for row in connection.execute(
            "SELECT a.assignment_id,a.study_code,a.condition_code,a.workflow_json,sp.participation_id "
            "FROM experiment_assignments a LEFT JOIN study_participations sp "
            "ON sp.study_code=a.study_code AND sp.participant_id=a.participant_id WHERE " + condition_a,
            params,
        ).fetchall()]
        counts["completed_experiments"] = 0
        saved_by_assignment: dict[str, dict[str, dict[str, Any]]] = {}
        for saved in connection.execute(
            "SELECT w.assignment_id,w.step_instance_id,w.status FROM experiment_workflow_progress w "
            "JOIN experiment_assignments a ON a.assignment_id=w.assignment_id WHERE " + condition_a,
            params,
        ).fetchall():
            saved_by_assignment.setdefault(str(saved["assignment_id"]), {})[str(saved["step_instance_id"])] = dict(saved)
        stages_by_participation: dict[str, dict[str, str]] = {}
        for saved in connection.execute(
            "SELECT sp.participation_id,g.stage_code,g.status FROM experiment_stage_progress g "
            "JOIN study_participations sp ON sp.participation_id=g.participation_id "
            "JOIN experiment_assignments a ON a.study_code=sp.study_code AND a.participant_id=sp.participant_id "
            "WHERE " + condition_a,
            params,
        ).fetchall():
            stages_by_participation.setdefault(str(saved["participation_id"]), {})[str(saved["stage_code"])] = str(saved["status"])
        for assignment in assignments:
            steps = _workflow_for_assignment(assignment)
            saved = saved_by_assignment.get(str(assignment["assignment_id"]), {})
            legacy = stages_by_participation.get(str(assignment.get("participation_id") or ""), {})
            if not saved:
                statuses = [legacy.get(_canonical_workflow_stage(step["module_code"]), "not_started") for step in steps]
            else:
                statuses = [saved.get(step["step_id"], {}).get("status", "not_started") for step in steps]
            if statuses and all(status == "completed" for status in statuses):
                counts["completed_experiments"] += 1
    return {"scope": scope, "study_code": STUDY_CODE if scope == "main" else TEST_STUDY_PREFIX + "*",
            "counts": counts,
            "warning": "This action permanently deletes rows in the selected study scope; participant master codes and instrument definitions are retained."}


@app.post("/api/admin/maintenance/reset")
def reset_study_data(
    data: MaintenanceResetIn,
    x_admin_token: str | None = Header(default=None),
):
    """Permanently remove only one explicitly confirmed study scope, keeping shared registries."""
    authorize(x_admin_token)
    expected_phrase = "CLEAR MAIN DATA" if data.scope == "main" else "CLEAR TEST DATA"
    if data.confirm_phrase != expected_phrase:
        raise HTTPException(422, "عبارت تأیید پاک‌سازی با محدودهٔ انتخاب‌شده مطابقت ندارد.")
    marker = "%s" if DATABASE_URL else "?"
    condition = "substr(study_code,1,12)='barsan-test-'" if data.scope == "test" else "study_code=" + marker
    params: tuple[Any, ...] = () if data.scope == "test" else (STUDY_CODE,)
    deleted: dict[str, int] = {}

    # The preview count and deletion occur within a single transaction; scoped child rows are
    # removed before their parent run/participation/assignment rows to respect foreign keys.
    with conn() as connection:
        if not DATABASE_URL:
            connection.execute("BEGIN IMMEDIATE")
        for table in ("experiment_group_links", "experiment_assignments"):
            connection.execute("SELECT " + ("group_link_id" if table.endswith("group_links") else "assignment_id")
                + " FROM " + table + " WHERE " + condition + " ORDER BY "
                + ("group_link_id" if table.endswith("group_links") else "assignment_id")
                + (" FOR UPDATE" if DATABASE_URL else ""), params).fetchall()
        assignment_query = "SELECT assignment_id FROM experiment_assignments WHERE " + condition
        run_query = "SELECT ir.run_id FROM instrument_runs ir JOIN study_participations sp ON sp.participation_id=ir.participation_id WHERE " + condition.replace("study_code", "sp.study_code")
        cursor = connection.execute("DELETE FROM experiment_group_claim_requests WHERE group_link_id IN (SELECT group_link_id FROM experiment_group_links WHERE "
            + condition + ") OR assignment_id IN (" + assignment_query + ")", (*params, *params))
        deleted["experiment_group_claim_requests"] = max(0, int(cursor.rowcount or 0))
        cursor = connection.execute("DELETE FROM experiment_stage_start_requests WHERE assignment_id IN (" + assignment_query
            + ") OR run_id IN (" + run_query + ")", (*params, *params))
        deleted["experiment_stage_start_requests"] = max(0, int(cursor.rowcount or 0))
        cursor = connection.execute("DELETE FROM experiment_time_event_requests WHERE assignment_id IN (" + assignment_query + ")", params)
        deleted["experiment_time_event_requests"] = max(0, int(cursor.rowcount or 0))
        for table in ("experiment_client_clock_checkpoints", "experiment_client_clocks"):
            cursor = connection.execute("DELETE FROM " + table + " WHERE run_id IN ("
                "SELECT ir.run_id FROM instrument_runs ir JOIN study_participations sp "
                "ON sp.participation_id=ir.participation_id WHERE "
                + condition.replace("study_code", "sp.study_code") + ")", params)
            deleted[table] = max(0, int(cursor.rowcount or 0))
        for name, query in (
            ("questionnaire_responses",
             "DELETE FROM questionnaire_responses WHERE run_id IN (SELECT ir.run_id FROM instrument_runs ir "
             "JOIN study_participations sp ON sp.participation_id=ir.participation_id WHERE "
             + condition.replace("study_code", "sp.study_code") + ")"),
            ("lifetrade_trials",
             "DELETE FROM lifetrade_trials WHERE run_id IN (SELECT ir.run_id FROM instrument_runs ir "
             "JOIN study_participations sp ON sp.participation_id=ir.participation_id WHERE "
             + condition.replace("study_code", "sp.study_code") + ")"),
            ("ultimatum_trials",
             "DELETE FROM ultimatum_trials WHERE run_id IN (SELECT ir.run_id FROM instrument_runs ir "
             "JOIN study_participations sp ON sp.participation_id=ir.participation_id WHERE "
             + condition.replace("study_code", "sp.study_code") + ")"),
            ("experiment_stage_progress",
             "DELETE FROM experiment_stage_progress WHERE participation_id IN "
             "(SELECT sp.participation_id FROM study_participations sp WHERE "
             + condition.replace("study_code", "sp.study_code") + ")"),
            ("experiment_consents",
             "DELETE FROM experiment_consents WHERE assignment_id IN "
             "(SELECT a.assignment_id FROM experiment_assignments a WHERE "
             + condition.replace("study_code", "a.study_code") + ")"),
            ("experiment_workflow_progress",
             "DELETE FROM experiment_workflow_progress WHERE assignment_id IN "
             "(SELECT a.assignment_id FROM experiment_assignments a WHERE "
             + condition.replace("study_code", "a.study_code") + ")"),
            ("experiment_time_tracking",
             "DELETE FROM experiment_time_tracking WHERE assignment_id IN "
             "(SELECT a.assignment_id FROM experiment_assignments a WHERE "
             + condition.replace("study_code", "a.study_code") + ")"),
            ("experiment_assignments",
             "DELETE FROM experiment_assignments WHERE " + condition),
            ("instrument_runs",
             "DELETE FROM instrument_runs WHERE participation_id IN "
             "(SELECT sp.participation_id FROM study_participations sp WHERE "
             + condition.replace("study_code", "sp.study_code") + ")"),
            ("study_participations",
             "DELETE FROM study_participations WHERE " + condition),
        ):
            cursor = connection.execute(query, params)
            deleted[name] = max(0, int(cursor.rowcount or 0))
        group_link_condition = (
            "substr(study_code,1,12)='barsan-test-'"
            if data.scope == "test"
            else "study_code=" + marker
        )
        group_link_params: tuple[Any, ...] = () if data.scope == "test" else (STUDY_CODE,)
        cursor = connection.execute(
            "DELETE FROM experiment_group_links WHERE " + group_link_condition,
            group_link_params,
        )
        deleted["group_links"] = max(0, int(cursor.rowcount or 0))
        if data.scope == "test":
            connection.execute("DELETE FROM study_workflows WHERE substr(study_code,1,12)='barsan-test-'")
            cursor = connection.execute(
                "DELETE FROM studies WHERE substr(study_code,1,12)='barsan-test-'"
            )
            deleted["test_studies"] = max(0, int(cursor.rowcount or 0))
        else:
            deleted["test_studies"] = 0
    return {"reset": True, "scope": data.scope, "deleted_rows": deleted,
            "participant_master_rows_retained": True,
            "message": "محدودهٔ انتخاب‌شده پاک شد؛ برای شروع دوباره باید لینک‌های اختصاصی جدید ساخته شوند."}


@app.get("/api/admin/progress")
def experiment_progress(
    x_admin_token: str | None = Header(default=None),
    scope: Literal["main", "test"] = "main",
    include_timing: bool = False,
):
    """Report completion against each invitation's exact ordered workflow snapshot."""
    authorize(x_admin_token)
    marker = "%s" if DATABASE_URL else "?"
    condition, params = _scope_filter(scope, "a.study_code", marker)
    base_query = (
        "SELECT a.assignment_id,a.study_code,p.participant_code,a.condition_code,a.assigned_at,"
        "a.workflow_json,sp.participation_id "
        "FROM experiment_assignments a "
        "JOIN participants p ON p.participant_id=a.participant_id "
        "LEFT JOIN study_participations sp ON sp.participant_id=a.participant_id AND sp.study_code=a.study_code "
        "WHERE " + condition + " AND a.active=1 ORDER BY a.assigned_at,p.participant_code"
    )
    workflow_query = (
        "SELECT w.assignment_id,w.step_instance_id,w.step_order,w.stage_code,w.status,w.attempt_number,"
        "w.started_at,w.completed_at,w.updated_at "
        "FROM experiment_workflow_progress w JOIN experiment_assignments a ON a.assignment_id=w.assignment_id "
        "WHERE " + condition.replace("a.study_code", "a.study_code") + " AND a.active=1 "
        "ORDER BY w.assignment_id,w.step_order"
    )
    stage_query = (
        "SELECT sp.participation_id,g.stage_code,g.status,g.attempt_number,g.started_at,g.completed_at,g.updated_at "
        "FROM experiment_stage_progress g JOIN study_participations sp ON sp.participation_id=g.participation_id "
        "JOIN experiment_assignments a ON a.study_code=sp.study_code AND a.participant_id=sp.participant_id "
        "WHERE " + condition + " AND a.active=1"
    )
    time_query = (
        "SELECT t.assignment_id,t.step_instance_id,t.stage_code,t.active_seconds,t.is_active,t.last_seen_at "
        "FROM experiment_time_tracking t JOIN experiment_assignments a ON a.assignment_id=t.assignment_id "
        "WHERE " + condition + " AND a.active=1"
    )
    with conn() as connection:
        raw_rows = [dict(row) for row in connection.execute(base_query, params).fetchall()]
        workflow_rows = [dict(row) for row in connection.execute(workflow_query, params).fetchall()]
        stage_rows = [dict(row) for row in connection.execute(stage_query, params).fetchall()]
        time_rows = [dict(row) for row in connection.execute(time_query, params).fetchall()] if include_timing else []
        client_times = [dict(row) for row in connection.execute(
            "SELECT t.assignment_id,t.step_instance_id,SUM(t.visible_ms) AS visible_ms,"
            "SUM(t.uncertain_ms) AS uncertain_ms,MAX(t.received_at) AS received_at "
            "FROM experiment_client_clocks t JOIN experiment_assignments a ON a.assignment_id=t.assignment_id WHERE "
            + condition + " AND a.active=1 GROUP BY t.assignment_id,t.step_instance_id", params).fetchall()] if include_timing else []

    workflow_by_assignment: dict[str, dict[str, dict[str, Any]]] = {}
    for saved in workflow_rows:
        workflow_by_assignment.setdefault(str(saved["assignment_id"]), {})[str(saved["step_instance_id"])] = saved
    stage_by_participation: dict[str, dict[str, dict[str, Any]]] = {}
    for saved in stage_rows:
        stage_by_participation.setdefault(str(saved["participation_id"]), {})[str(saved["stage_code"])] = saved
    time_by_assignment: dict[str, dict[str, dict[str, Any]]] = {}
    current_time = datetime.now(timezone.utc)
    for timed in time_rows:
        assignment_id = str(timed["assignment_id"])
        seconds = float(timed["active_seconds"] or 0)
        if int(timed["is_active"] or 0):
            try:
                last_seen = datetime.fromisoformat(str(timed["last_seen_at"]).replace("Z", "+00:00"))
                if last_seen.tzinfo is None:
                    last_seen = last_seen.replace(tzinfo=timezone.utc)
                seconds += min(45.0, max(0.0, current_time.timestamp() - last_seen.timestamp()))
            except (TypeError, ValueError):
                pass
        time_by_assignment.setdefault(assignment_id, {})[str(timed["step_instance_id"])] = {
            "stage_code": timed["stage_code"],
            "active_seconds": round(seconds, 2),
            "last_seen_at": timed["last_seen_at"],
        }

    for observed in client_times:
        timing = time_by_assignment.setdefault(str(observed["assignment_id"]), {}).setdefault(str(observed["step_instance_id"]), {})
        timing["legacy_active_seconds"] = timing.get("active_seconds", 0)
        timing["active_seconds"] = round(observed["visible_ms"] / 1000, 2)
        timing["uncertain_seconds"] = round(observed["uncertain_ms"] / 1000, 2)
        timing["time_source"] = "client_visible_v2"
        timing["last_seen_at"] = observed["received_at"]

    stale_cutoff = current_time.timestamp() - 24 * 60 * 60

    def parse_time(value: Any) -> float | None:
        if value is None:
            return None
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.timestamp()
        except (TypeError, ValueError):
            return None

    output = []
    for row in raw_rows:
        assignment_id = str(row["assignment_id"])
        participation_id = str(row.get("participation_id") or "")
        steps = _workflow_for_assignment(row)
        saved_steps = workflow_by_assignment.get(assignment_id, {})
        legacy_stages = stage_by_participation.get(participation_id, {})
        timings = time_by_assignment.get(assignment_id, {})
        module_times: dict[str, float] = {}
        progress_steps = []
        last_activity_candidates = [parse_time(row.get("assigned_at"))]
        has_started = False
        for index, step in enumerate(steps):
            code = step["module_code"]
            stage_code = _canonical_workflow_stage(code)
            saved = saved_steps.get(step["step_id"])
            # Pre-workflow and legacy module-only invitations have stage-level records only.
            if saved is None and not saved_steps:
                saved = legacy_stages.get(stage_code)
            status = str((saved or {}).get("status") or "not_started")
            attempts = int((saved or {}).get("attempt_number") or 0)
            timing = timings.get(step["step_id"], {})
            seconds = float(timing.get("active_seconds") or 0)
            module_times[code] = round(module_times.get(code, 0.0) + seconds, 2)
            has_started = has_started or attempts > 0 or status != "not_started"
            progress_steps.append({
                "step_id": step["step_id"],
                "step_order": index + 1,
                "module_code": code,
                "stage_code": stage_code,
                "module_label": _workflow_module_label(code),
                "status": status,
                "attempt_number": attempts,
                "started_at": (saved or {}).get("started_at"),
                "completed_at": (saved or {}).get("completed_at"),
                "updated_at": (saved or {}).get("updated_at"),
                "active_seconds": round(seconds, 2) if include_timing else None,
                "time_source": timing.get("time_source", "server_arrival_legacy") if include_timing else None,
                "legacy_active_seconds": timing.get("legacy_active_seconds", seconds) if include_timing else None,
                "uncertain_seconds": timing.get("uncertain_seconds", 0) if include_timing else None,
            })
            for field in ("started_at", "completed_at", "updated_at"):
                last_activity_candidates.append(parse_time((saved or {}).get(field)))
            last_activity_candidates.append(parse_time(timing.get("last_seen_at")))

        total_steps = len(progress_steps)
        completed_steps = sum(item["status"] == "completed" for item in progress_steps)
        current_step = next((item for item in progress_steps if item["status"] != "completed"), None)
        row["workflow_steps"] = progress_steps
        row["ultimatum_variant"] = _workflow_ultimatum_variant(steps)
        row["total_steps"] = total_steps
        row["completed_steps"] = completed_steps
        row["completed_stages"] = completed_steps
        row["progress_percent"] = round(100 * completed_steps / total_steps) if total_steps else 0
        row["workflow_complete"] = bool(total_steps and completed_steps == total_steps)
        row["current_step_id"] = current_step["step_id"] if current_step else None
        row["current_stage"] = current_step["module_code"] if current_step else None
        row["current_stage_label"] = current_step["module_label"] if current_step else "روند کامل"
        if include_timing:
            row["module_time_seconds"] = module_times
            row["total_active_seconds"] = round(sum(module_times.values()), 2)
        for canonical in STAGE_ORDER:
            related = [item for item in progress_steps if item["stage_code"] == canonical]
            if related:
                row[canonical + "_status"] = (
                    "completed" if all(item["status"] == "completed" for item in related)
                    else "in_progress" if any(item["status"] != "not_started" for item in related)
                    else "not_started"
                )
                row[canonical + "_attempts"] = sum(item["attempt_number"] for item in related)
            else:
                stage = legacy_stages.get(canonical, {})
                row[canonical + "_status"] = stage.get("status", "not_started")
                row[canonical + "_attempts"] = int(stage.get("attempt_number") or 0)

        valid_times = [value for value in last_activity_candidates if value is not None]
        last_epoch = max(valid_times) if valid_times else None
        row["last_activity_at"] = (
            datetime.fromtimestamp(last_epoch, timezone.utc).isoformat()
            if last_epoch is not None else row.get("assigned_at")
        )
        row["hours_since_activity"] = (
            round(max(0.0, (current_time.timestamp() - last_epoch) / 3600), 2)
            if last_epoch is not None else None
        )
        if total_steps and completed_steps == total_steps:
            row["progress_state"] = "completed"
        elif not has_started:
            row["progress_state"] = "not_started"
        elif last_epoch is not None and last_epoch < stale_cutoff:
            row["progress_state"] = "stale_incomplete"
        else:
            row["progress_state"] = "in_progress"
        row.pop("workflow_json", None)
        row.pop("participation_id", None)
        row.pop("assignment_id", None)
        output.append(row)
    return output


@app.get("/api/admin/ultimatum/export.csv")
def export_ultimatum_csv(
    x_admin_token: str | None = Header(default=None),
    scope: Literal["main", "test"] = "main",
):
    """Export Ultimatum attempts from the main study or isolated test studies."""
    authorize(x_admin_token)
    marker = "%s" if DATABASE_URL else "?"
    scope_filter = "substr(sp.study_code,1,12)='barsan-test-'" if scope == "test" else "sp.study_code=" + marker
    query = (
        "SELECT sp.study_code,p.participant_code,a.condition_code,ir.run_id,ir.status AS run_status,"
        "ir.started_at AS run_started_at,ir.completed_at AS run_completed_at,"
        "ir.metadata AS run_metadata,usp.current_run_id AS current_stage_run_id,"
        "usp.draft_payload AS final_stage_payload,iv.version AS instrument_version,"
        "u.round_id,u.phase,u.block_code,u.block_order,u.trial_in_block,u.trial_sequence,"
        "u.role,u.opponent_condition,u.opponent_actual_type,u.status AS round_status,"
        "u.participant_share,u.opponent_share,u.participant_decision,u.opponent_decision,u.accepted,"
        "u.points_earned,u.potential_points_lost,u.reaction_time_ms,u.wait_duration_ms,"
        "u.started_at,u.decision_screen_shown_at,u.responded_at,u.received_at,u.completed_at,u.payload "
        "FROM ultimatum_trials u "
        "JOIN instrument_runs ir ON ir.run_id=u.run_id "
        "JOIN study_participations sp ON sp.participation_id=ir.participation_id "
        "JOIN participants p ON p.participant_id=sp.participant_id "
        "JOIN instrument_versions iv ON iv.instrument_version_id=ir.instrument_version_id "
        "LEFT JOIN experiment_assignments a ON a.participant_id=p.participant_id "
        "AND a.study_code=sp.study_code "
        "LEFT JOIN experiment_stage_progress usp ON usp.participation_id=sp.participation_id "
        "AND usp.stage_code='ultimatum' "
        "WHERE " + scope_filter + " ORDER BY ir.started_at,u.phase,u.block_order,u.trial_sequence"
    )
    with conn() as connection:
        rows = connection.execute(query, () if scope == "test" else (STUDY_CODE,)).fetchall()

    fields = [
        "study_code", "participant_code", "condition_code", "run_id", "run_status", "run_started_at",
        "run_completed_at", "attempt_number", "instrument_version", "round_id", "phase",
        "block_code", "block_order", "trial_in_block", "trial_sequence", "role",
        "opponent_condition", "opponent_actual_type", "round_status", "ultimatum_variant",
        "workflow_module_code", "participant_share",
        "opponent_share", "participant_decision", "opponent_decision", "accepted",
        "points_earned", "potential_points_lost", "reaction_time_ms", "wait_duration_ms",
        "started_at", "decision_screen_shown_at", "responded_at", "received_at",
        "completed_at", "server_resolved_at", "feedback_group", "final_score",
        "final_potential_points_lost", "block_order_json", "final_stage_payload_json",
        "run_metadata_json", "client_metadata_json", "client_metadata_resolve_json",
        "client_event_timestamps_json", "client_event_server_received_at",
        "proposer_screen_shown_at", "slider_first_changed_at", "slider_last_changed_at",
        "offer_submitted_at", "waiting_started_at", "waiting_ended_at",
        "opponent_offer_generated_at", "opponent_response_generated_at",
        "responder_offer_shown_at", "participant_choice_at", "result_screen_shown_at",
        "next_trial_clicked_at", "advance_clicked_at",
        "opponent_model", "offer_model_json", "decision_model_json", "normal_mean",
        "normal_sd", "normal_ai_self_share", "normal_participant_share",
        "binomial_n", "binomial_p", "binomial_draw", "binomial_accept_threshold",
        "participant_offer_to_opponent", "participant_offer_to_self",
        "computer_offer_to_participant", "computer_offer_to_self", "payload_json",
    ]

    def generate_csv():
        buffer = io.StringIO(newline="")
        buffer.write("\ufeff")
        writer = csv.DictWriter(buffer, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        yield buffer.getvalue()
        buffer.seek(0)
        buffer.truncate(0)
        for raw in rows:
            item = dict(raw)
            run_metadata = _json_value(item.pop("run_metadata", {}))
            current_run_id = item.pop("current_stage_run_id", None)
            stage_payload = _json_value(item.pop("final_stage_payload", {}))
            condition_code = str(item.get("condition_code") or "").strip().upper()
            current_completed_run = (
                current_run_id == item.get("run_id") and item.get("run_status") == "completed"
            )
            item["attempt_number"] = _int_or_none(run_metadata.get("attempt_number"))
            item["workflow_module_code"] = run_metadata.get("workflow_module_code") or "ultimatum"
            item["ultimatum_variant"] = (
                run_metadata.get("ultimatum_variant")
                or ULTIMATUM_WORKFLOW_VARIANTS.get(run_metadata.get("workflow_module_code"))
                or ""
            )
            item["feedback_group"] = item["ultimatum_variant"] or (
                "Frame" if condition_code in {"A", "FRAME", "FRAMING"} else "Control"
            )
            item["block_order_json"] = json.dumps(
                run_metadata.get("block_order") or [], ensure_ascii=False, separators=(",", ":")
            )
            final_payload = stage_payload if current_completed_run else {}
            item["final_score"] = final_payload.get("final_score")
            item["final_potential_points_lost"] = final_payload.get("total_potential_points_lost")
            item["final_stage_payload_json"] = json.dumps(
                final_payload, ensure_ascii=False, separators=(",", ":")
            )
            item["run_metadata_json"] = json.dumps(
                run_metadata, ensure_ascii=False, separators=(",", ":")
            )

            payload = _json_value(item.pop("payload", {}))
            item["client_event_timestamps_json"] = json.dumps(
                payload.get("client_event_timestamps") or {}, ensure_ascii=False, separators=(",", ":")
            )
            item["client_event_server_received_at"] = payload.get("client_event_server_received_at")
            event_timestamps = payload.get("client_event_timestamps") or {}
            for event_name in (
                "proposer_screen_shown_at", "slider_first_changed_at", "slider_last_changed_at",
                "offer_submitted_at", "waiting_started_at", "waiting_ended_at",
                "responder_offer_shown_at", "participant_choice_at", "result_screen_shown_at",
                "next_trial_clicked_at", "advance_clicked_at",
            ):
                item[event_name] = event_timestamps.get(event_name)
            item["opponent_offer_generated_at"] = payload.get("opponent_offer_generated_at")
            item["opponent_response_generated_at"] = payload.get("opponent_response_generated_at")
            offer_model = _json_value(payload.get("offer_model"))
            decision_model = _json_value(payload.get("decision_model"))
            item["opponent_model"] = (
                decision_model.get("model") or offer_model.get("model") or ""
            )
            item["offer_model_json"] = json.dumps(
                offer_model, ensure_ascii=False, separators=(",", ":")
            ) if offer_model else ""
            item["decision_model_json"] = json.dumps(
                decision_model, ensure_ascii=False, separators=(",", ":")
            ) if decision_model else ""
            item["normal_mean"] = offer_model.get("mean")
            item["normal_sd"] = offer_model.get("sd")
            item["normal_ai_self_share"] = offer_model.get("sampled_ai_self_share")
            item["normal_participant_share"] = offer_model.get("sampled_participant_share")
            item["binomial_n"] = decision_model.get("n")
            item["binomial_p"] = decision_model.get("p")
            item["binomial_draw"] = decision_model.get("draw")
            item["binomial_accept_threshold"] = decision_model.get("accept_threshold")
            for field in (
                "participant_offer_to_opponent", "participant_offer_to_self",
                "computer_offer_to_participant", "computer_offer_to_self", "server_resolved_at",
            ):
                item[field] = payload.get(field)
            item["client_metadata_json"] = json.dumps(
                payload.get("client_metadata") or {}, ensure_ascii=False, separators=(",", ":")
            )
            item["client_metadata_resolve_json"] = json.dumps(
                payload.get("client_metadata_resolve") or {}, ensure_ascii=False, separators=(",", ":")
            )
            item["payload_json"] = json.dumps(
                payload, ensure_ascii=False, separators=(",", ":")
            )
            writer.writerow(item)
            yield buffer.getvalue()
            buffer.seek(0)
            buffer.truncate(0)

    return StreamingResponse(
        generate_csv(),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": 'attachment; filename="' + ("barsan_test_ultimatum_trials.csv" if scope == "test" else "barsan_ultimatum_trials.csv") + '"',
            "Cache-Control": "no-store",
        },
    )


@app.get("/api/admin/questionnaire/export.csv")
def export_questionnaire_csv(
    x_admin_token: str | None = Header(default=None),
    scope: Literal["main", "test"] = "main",
):
    """Export completed questionnaire answers in long format for one data scope."""
    authorize(x_admin_token)
    marker = "%s" if DATABASE_URL else "?"
    scope_filter = "substr(sp.study_code,1,12)='barsan-test-'" if scope == "test" else "sp.study_code=" + marker
    query = (
        "SELECT sp.study_code,p.participant_code,a.condition_code,qr.question_code,qr.response_value,"
        "qr.updated_at,qr.run_id,iv.version AS instrument_version "
        "FROM questionnaire_responses qr "
        "JOIN instrument_runs ir ON ir.run_id=qr.run_id "
        "JOIN instrument_versions iv ON iv.instrument_version_id=ir.instrument_version_id "
        "JOIN study_participations sp ON sp.participation_id=ir.participation_id "
        "JOIN participants p ON p.participant_id=sp.participant_id "
        "LEFT JOIN experiment_assignments a ON a.participant_id=p.participant_id "
        "AND a.study_code=sp.study_code "
        "WHERE iv.instrument_code=" + marker + " AND " + scope_filter
        + " AND ir.status='completed' "
        "ORDER BY p.participant_code,qr.question_code"
    )
    with conn() as connection:
        params = ("questionnaire",) if scope == "test" else ("questionnaire", STUDY_CODE)
        rows = connection.execute(query, params).fetchall()

    def generate_csv():
        buffer = io.StringIO(newline="")
        buffer.write("\ufeff")
        fields = [
            "study_code", "participant_code", "condition_code", "question_code", "response_value",
            "updated_at", "run_id", "instrument_version",
        ]
        writer = csv.DictWriter(buffer, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        yield buffer.getvalue()
        buffer.seek(0)
        buffer.truncate(0)
        for row in rows:
            writer.writerow(dict(row))
            yield buffer.getvalue()
            buffer.seek(0)
            buffer.truncate(0)

    return StreamingResponse(
        generate_csv(),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": 'attachment; filename="' + ("barsan_test_questionnaire_responses.csv" if scope == "test" else "barsan_questionnaire_responses.csv") + '"',
            "Cache-Control": "no-store",
        },
    )


@app.get("/api/admin/sessions")
def sessions(
    x_admin_token: str | None = Header(default=None),
    scope: Literal["main", "test"] = "main",
):
    authorize(x_admin_token)
    marker = "%s" if DATABASE_URL else "?"
    study_filter = "substr(sp.study_code,1,12)='barsan-test-'" if scope == "test" else "sp.study_code=" + marker
    params = (INSTRUMENT_CODE,) if scope == "test" else (INSTRUMENT_CODE, STUDY_CODE)
    with conn() as connection:
        rows = connection.execute(
            "SELECT sp.study_code,ir.run_id AS session_id,p.participant_code,ir.started_at AS created_at,"
            "ir.completed_at,ir.status,ir.trial_count,iv.instrument_code,iv.version AS experiment_version "
            "FROM instrument_runs ir "
            "JOIN study_participations sp ON sp.participation_id=ir.participation_id "
            "JOIN participants p ON p.participant_id=sp.participant_id "
            "JOIN instrument_versions iv ON iv.instrument_version_id=ir.instrument_version_id "
            "JOIN instruments i ON i.instrument_code=iv.instrument_code "
            "WHERE i.instrument_code=" + marker + " AND " + study_filter + " ORDER BY ir.started_at DESC",
            params,
        ).fetchall()
    return [dict(row) for row in rows]


@app.get("/api/admin/export.csv")
def export_csv(
    x_admin_token: str | None = Header(default=None),
    scope: Literal["main", "test"] = "main",
):
    """Stream Life-Trade trial rows from the main study or isolated test studies."""
    authorize(x_admin_token)
    marker = "%s" if DATABASE_URL else "?"
    scope_filter = "substr(sp.study_code,1,12)='barsan-test-'" if scope == "test" else "sp.study_code=" + marker

    with conn() as connection:
        key_query = (
            "SELECT t.payload FROM lifetrade_trials t "
            "JOIN instrument_runs ir ON ir.run_id=t.run_id "
            "JOIN study_participations sp ON sp.participation_id=ir.participation_id "
            "WHERE ir.status <> 'superseded' AND " + scope_filter
        )
        key_params = () if scope == "test" else (STUDY_CODE,)
        key_rows = connection.execute(key_query, key_params).fetchall()

    payload_fields = set()
    for key_row in key_rows:
        payload = key_row["payload"]
        parsed = json.loads(payload) if isinstance(payload, str) else payload
        if isinstance(parsed, dict):
            payload_fields.update(parsed.keys())

    fields = sorted(
        payload_fields
        | {"server_received_at", "server_run_id", "server_session_id", "participant_code",
           "instrument_code", "instrument_version", "study_code"}
    )

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
            query = (
                "SELECT t.client_event_id,t.run_id,t.received_at,t.payload,p.participant_code,"
                "iv.version,sp.study_code "
                "FROM lifetrade_trials t "
                "JOIN instrument_runs ir ON ir.run_id=t.run_id "
                "JOIN study_participations sp ON sp.participation_id=ir.participation_id "
                "JOIN participants p ON p.participant_id=sp.participant_id "
                "JOIN instrument_versions iv ON iv.instrument_version_id=ir.instrument_version_id "
                "JOIN instruments i ON i.instrument_code=iv.instrument_code "
                "WHERE i.instrument_code=" + marker + " AND " + scope_filter +
                " AND ir.status <> 'superseded'"
            )
            params = [INSTRUMENT_CODE]
            if scope != "test":
                params.append(STUDY_CODE)
            if last_received_at is not None:
                query += (
                    " AND (t.received_at,t.client_event_id) > (" + marker + "," + marker + ")"
                )
                params.extend([last_received_at, last_event_id])
            query += " ORDER BY t.received_at,t.client_event_id LIMIT 500"

            with conn() as connection:
                rows = connection.execute(query, tuple(params)).fetchall()
            if not rows:
                break
            last_received_at = rows[-1]["received_at"]
            last_event_id = rows[-1]["client_event_id"]
            output_rows = []
            for row in rows:
                item = json.loads(row["payload"]) if isinstance(row["payload"], str) else dict(row["payload"])
                item["server_received_at"] = row["received_at"]
                item["server_run_id"] = row["run_id"]
                item["server_session_id"] = row["run_id"]
                item["participant_code"] = row["participant_code"]
                item["instrument_code"] = INSTRUMENT_CODE
                item["instrument_version"] = row["version"]
                item["study_code"] = row["study_code"]
                output_rows.append(item)
            writer.writerows(output_rows)
            yield buffer.getvalue()
            buffer.seek(0)
            buffer.truncate(0)

    filename = "barsan_test_lifetrade_trials.csv" if scope == "test" else "lifetrade_trials.csv"
    return StreamingResponse(
        generate_csv(),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
        },
    )


async def _read_streaming_response(response: StreamingResponse) -> bytes:
    chunks = []
    async for chunk in response.body_iterator:
        chunks.append(chunk.encode("utf-8") if isinstance(chunk, str) else bytes(chunk))
    return b"".join(chunks)


def _ordered_task_export(module: str, scope: str):
    """Bounded keyset reads release the DB connection before yielding to a slow client."""
    marker = "%s" if DATABASE_URL else "?"
    table, key = (("lifetrade_trials", "client_event_id") if module == "dd" else ("ultimatum_trials", "round_id"))
    fields = DD_FIELDS if module == "dd" else UT_FIELDS
    def encode(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    def generate():
        buffer = io.StringIO(newline="")
        writer = csv.DictWriter(buffer, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        yield buffer.getvalue()
        cursor = ""
        while True:
            params = (cursor,)
            with conn() as connection:
                rows = connection.execute(
                    "SELECT trial.*,sp.study_code,p.participant_code,a.condition_code,"
                    "run.status AS run_status,run.started_at AS run_started_at,run.completed_at AS run_completed_at,"
                    "run.metadata AS run_metadata,version.version AS instrument_version "
                    "FROM (SELECT * FROM " + table + " WHERE " + key + ">" + marker
                    + " ORDER BY " + key + " LIMIT 200) AS trial "
                    "JOIN instrument_runs AS run ON run.run_id=trial.run_id "
                    "JOIN instrument_versions AS version ON version.instrument_version_id=run.instrument_version_id "
                    "JOIN study_participations AS sp ON sp.participation_id=run.participation_id "
                    "JOIN participants AS p ON p.participant_id=sp.participant_id "
                    "LEFT JOIN experiment_assignments AS a ON a.participant_id=sp.participant_id AND a.study_code=sp.study_code "
                    "ORDER BY trial." + key, params,
                ).fetchall()
            if not rows:
                break
            buffer.seek(0);buffer.truncate(0)
            for raw in rows:
                if not (str(raw["study_code"]).startswith(TEST_STUDY_PREFIX) if scope == "test" else raw["study_code"] == STUDY_CODE):
                    continue
                item = dict(raw)
                payload = _json_value(item["payload"])
                metadata = _json_value(item.pop("run_metadata"))
                item.update(schema_version=2, attempt_number=metadata.get("attempt_number", 1),
                            server_received_at=item.get("received_at"))
                if module == "dd":
                    item.update(workflow_module_code=metadata.get("workflow_module_code", "dd_lifetrade"),
                                workflow_step_id=metadata.get("workflow_step_id"))
                    record = dd_record(item, payload)
                    item.update({name: payload.get(name) for name in DD_FIELDS if name in payload and name not in {*COMMON_FIELDS, "client_event_id", "server_received_at", "workflow_module_code", "workflow_step_id"}})
                    item.update(stimulus_index=payload.get("index"),
                                left_stimulus=record["stimulus"]["left"],right_stimulus=record["stimulus"]["right"],
                                stimulus_parameters_json=encode(record["stimulus"]["parameters"]),
                                adaptive_path_json=encode(record["adaptive_path"]))
                else:
                    item["round_status"] = item["status"]
                    record = ultimatum_record(item, payload)
                    item.update(planned_wait_duration_ms=payload.get("planned_wait_duration_ms"),
                                events_json=encode(record["timing"]["events"]),
                                model_json=encode(record["model"]),device_json=encode(record["device"]))
                item.update(record_json=encode(record), raw_payload_json=encode(payload))
                writer.writerow(item)
            cursor = rows[-1][key]
            if buffer.tell():
                yield buffer.getvalue()
    return StreamingResponse(generate(),media_type="text/csv; charset=utf-8",headers={
        "Content-Disposition": 'attachment; filename="barsan_' + module + '_records_v2_' + scope + '.csv"',
        "Cache-Control": "no-store",
    })


@app.get("/api/admin/dd/records.csv")
def export_dd_records(x_admin_token: str | None = Header(default=None), scope: Literal["main", "test"] = "main"):
    authorize(x_admin_token)
    return _ordered_task_export("dd", scope)


@app.get("/api/admin/ultimatum/records.csv")
def export_ultimatum_records(x_admin_token: str | None = Header(default=None), scope: Literal["main", "test"] = "main"):
    authorize(x_admin_token)
    return _ordered_task_export("ultimatum", scope)


@app.get("/api/admin/time/checkpoints.csv")
def export_client_time(x_admin_token: str | None = Header(default=None), scope: Literal["main", "test"] = "main"):
    authorize(x_admin_token)
    marker = "%s" if DATABASE_URL else "?"
    def generate():
        fields = ["schema_version", "study_code", "participant_code", "run_id", "run_status", "step_instance_id",
                  "stage_code", "clock_id", "sequence", "visible_ms", "uncertain_ms", "closed", "received_at"]
        buffer = io.StringIO(newline="")
        writer = csv.DictWriter(buffer, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        yield buffer.getvalue()
        cursor = ("", 0)
        while True:
            params = cursor
            with conn() as connection:
                rows = connection.execute(
                    "SELECT c.*,t.clock_id,t.step_instance_id,t.stage_code,r.status AS run_status,"
                    "sp.study_code,p.participant_code FROM (SELECT * FROM experiment_client_clock_checkpoints "
                    "WHERE (run_id,sequence)>(" + marker + "," + marker + ") ORDER BY run_id,sequence LIMIT 200) c "
                    "JOIN experiment_client_clocks t USING(run_id) JOIN instrument_runs r USING(run_id) "
                    "JOIN study_participations sp USING(participation_id) JOIN participants p USING(participant_id) "
                    "ORDER BY c.run_id,c.sequence", params).fetchall()
            if not rows:
                break
            cursor = (rows[-1]["run_id"], rows[-1]["sequence"])
            buffer.seek(0); buffer.truncate(0)
            writer.writerows({"schema_version": 2, **dict(row)} for row in rows
                if (str(row["study_code"]).startswith(TEST_STUDY_PREFIX) if scope == "test" else row["study_code"] == STUDY_CODE))
            if buffer.tell():
                yield buffer.getvalue()
    return StreamingResponse(generate(), media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="barsan_time_checkpoints_v2.csv"', "Cache-Control": "no-store"})


@app.get("/api/admin/export-all.zip")
def export_all_modules_zip(
    x_admin_token: str | None = Header(default=None),
    scope: Literal["main", "test"] = "main",
):
    """Download all three module exports and current participant progress in one ZIP."""
    authorize(x_admin_token)
    # Reuse the exact same scoped export functions as the individual CSV buttons.
    responses = [
        ("questionnaire.csv", export_questionnaire_csv(x_admin_token, scope)),
        ("lifetrade.csv", export_csv(x_admin_token, scope)),
        ("ultimatum.csv", export_ultimatum_csv(x_admin_token, scope)),
        ("time_checkpoints_v2.csv", export_client_time(x_admin_token, scope)),
    ]
    archive_buffer = io.BytesIO()
    with zipfile.ZipFile(archive_buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
        for filename, response in responses:
            archive.writestr(filename, asyncio.run(_read_streaming_response(response)))
        participant_rows = experiment_progress(x_admin_token, scope)
        progress_buffer = io.StringIO(newline="")
        progress_buffer.write("\ufeff")
        progress_fields = sorted({
            field for row in participant_rows for field in row.keys()
        } or {"study_code", "participant_code", "condition_code", "progress_state"})
        progress_writer = csv.DictWriter(progress_buffer, fieldnames=progress_fields, extrasaction="ignore")
        progress_writer.writeheader()
        progress_writer.writerows(participant_rows)
        archive.writestr("participant_progress.csv", progress_buffer.getvalue().encode("utf-8"))
        archive.writestr(
            "README.txt",
            "BarSan Experiment export\n"
            + "Scope: " + scope + "\n"
            + ("Test data only (study_code starts with barsan-test-)." if scope == "test"
               else "Main study only (study_code equals configured STUDY_CODE).")
            + "\nFiles are CSV UTF-8; keep the archive confidential.\n",
        )
    filename = "barsan_test_full_export.zip" if scope == "test" else "barsan_main_full_export.zip"
    return StreamingResponse(
        io.BytesIO(archive_buffer.getvalue()),
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
        },
    )


app.mount("/Stimuli", StaticFiles(directory=os.path.join(ROOT, "app", "Stimuli")), name="stimuli")


@lru_cache(maxsize=1)
def load_homepage() -> str:
    with open(os.path.join(ROOT, "app", "index.html"), encoding="utf-8") as file:
        html = file.read()
    with open(os.path.join(ROOT, "app", "persistence.js"), encoding="utf-8") as file:
        persistence = file.read()
    with open(os.path.join(ROOT, "app", "task_timing.js"), encoding="utf-8") as file:
        timing = file.read()
    return html.replace("<!-- BARSAN_PERSISTENCE -->", "<script>" + persistence + "</script><script>" + timing + "</script>", 1)


@app.get("/admin", response_class=HTMLResponse)
@app.get("/admin/links", response_class=HTMLResponse)
def admin_links_page(request: Request):
    """Serve the protected-action UI; actual link creation/export still requires ADMIN_TOKEN."""
    path = os.path.join(ROOT, "app", "admin-links.html")
    try:
        with open(path, encoding="utf-8") as file:
            return HTMLResponse(file.read().replace("__BARSAN_FONT_URL__",
                request.scope.get("root_path", "").rstrip("/") + "/assets/vazirmatn-v33.003.woff2"))
    except OSError as exc:
        raise HTTPException(503, "Admin link page is not installed on this server.") from exc


@app.get("/assets/vazirmatn-v33.003.woff2")
def task_font():
    return FileResponse(os.path.join(ROOT, "app", "fonts", "vazirmatn-v33.003.woff2"),
                        media_type="font/woff2", headers={"Cache-Control": "public,max-age=31536000,immutable"})


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    root_path = request.scope.get("root_path", "").rstrip("/")
    return load_homepage().replace("__BARSAN_FONT_URL__", root_path + "/assets/vazirmatn-v33.003.woff2")
