import csv
import hashlib
import io
import json
import math
import os
import random
import secrets
import sqlite3
import uuid
import zipfile
from contextlib import contextmanager
from datetime import datetime, timezone
from functools import lru_cache
from typing import Any, Iterator, Literal

from fastapi import FastAPI, Header, HTTPException, Request
from cryptography.fernet import Fernet, InvalidToken
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

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
}
STAGE_ORDER = ("questionnaire", "dd_lifetrade", "ultimatum")
ULTIMATUM_TOTAL_POINTS = 10
ULTIMATUM_MAIN_BLOCK_TRIALS = 5
ULTIMATUM_MAIN_BLOCK_COUNT = 4
ULTIMATUM_MAIN_TRIALS = ULTIMATUM_MAIN_BLOCK_TRIALS * ULTIMATUM_MAIN_BLOCK_COUNT
# Provisional, auditable opponent models. These can be adjusted after protocol confirmation.
ULTIMATUM_AI_OFFER_NORMAL_MEAN = 5.0
ULTIMATUM_AI_OFFER_NORMAL_SD = 2.0
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
            "application_name": "barsan-experiment-api",
        },
        open=True,
    )

    @contextmanager
    def conn() -> Iterator[Any]:
        try:
            with DB_POOL.connection(timeout=DB_POOL_TIMEOUT) as connection:
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
                "invitation_token_encrypted TEXT, workflow_json TEXT NOT NULL DEFAULT '[\"questionnaire\",\"dd_lifetrade\",\"ultimatum\']', "
                "first_opened_at TEXT, last_opened_at TEXT, active INTEGER NOT NULL DEFAULT 1, assigned_at TEXT NOT NULL, "
                "UNIQUE(study_code, participant_id))"
            )
            connection.execute("ALTER TABLE experiment_assignments ADD COLUMN IF NOT EXISTS invitation_token_encrypted TEXT")
            connection.execute("ALTER TABLE experiment_assignments ADD COLUMN IF NOT EXISTS workflow_json TEXT NOT NULL DEFAULT '[\"questionnaire\",\"dd_lifetrade\",\"ultimatum\']'")
            connection.execute("ALTER TABLE experiment_assignments ADD COLUMN IF NOT EXISTS first_opened_at TEXT")
            connection.execute("ALTER TABLE experiment_assignments ADD COLUMN IF NOT EXISTS last_opened_at TEXT")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS experiment_consents ("
                "assignment_id TEXT NOT NULL REFERENCES experiment_assignments(assignment_id), "
                "consent_version TEXT NOT NULL, accepted_at TEXT NOT NULL, "
                "PRIMARY KEY(assignment_id, consent_version))"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS experiment_workflow_steps ("
                "participation_id TEXT NOT NULL REFERENCES study_participations(participation_id), "
                "step_index INTEGER NOT NULL, module_code TEXT NOT NULL CHECK(module_code IN ('questionnaire','dd_lifetrade','ultimatum')), "
                "status TEXT NOT NULL DEFAULT 'not_started' CHECK(status IN ('not_started','in_progress','completed')), "
                "attempt_number INTEGER NOT NULL DEFAULT 0, current_run_id TEXT REFERENCES instrument_runs(run_id), "
                "draft_payload JSONB NOT NULL DEFAULT '{}'::jsonb, started_at TEXT, completed_at TEXT, updated_at TEXT NOT NULL, "
                "PRIMARY KEY(participation_id,step_index), UNIQUE(current_run_id))"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS study_workflow_configs ("
                "study_code TEXT PRIMARY KEY REFERENCES studies(study_code), workflow_json TEXT NOT NULL, updated_at TEXT NOT NULL)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS experiment_activity_pings ("
                "ping_id TEXT PRIMARY KEY, assignment_id TEXT NOT NULL REFERENCES experiment_assignments(assignment_id), "
                "run_id TEXT REFERENCES instrument_runs(run_id), workflow_step_index INTEGER, active_seconds REAL NOT NULL CHECK(active_seconds BETWEEN 0 AND 60), "
                "received_at TEXT NOT NULL)"
            )
            connection.execute("ALTER TABLE experiment_activity_pings ADD COLUMN IF NOT EXISTS workflow_step_index INTEGER")
            connection.execute("CREATE INDEX IF NOT EXISTS idx_activity_pings_assignment ON experiment_activity_pings(assignment_id,received_at)")
            connection.execute("CREATE INDEX IF NOT EXISTS idx_activity_pings_run ON experiment_activity_pings(run_id,received_at)")
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
                "invitation_token_encrypted TEXT, workflow_json TEXT NOT NULL DEFAULT '[\"questionnaire\",\"dd_lifetrade\",\"ultimatum\']', "
                "first_opened_at TEXT, last_opened_at TEXT, active INTEGER NOT NULL DEFAULT 1, assigned_at TEXT NOT NULL, "
                "UNIQUE(study_code, participant_id))"
            )
            assignment_columns = {row["name"] for row in connection.execute("PRAGMA table_info(experiment_assignments)").fetchall()}
            if "invitation_token_encrypted" not in assignment_columns:
                connection.execute("ALTER TABLE experiment_assignments ADD COLUMN invitation_token_encrypted TEXT")
            if "workflow_json" not in assignment_columns:
                connection.execute("ALTER TABLE experiment_assignments ADD COLUMN workflow_json TEXT NOT NULL DEFAULT '[\"questionnaire\",\"dd_lifetrade\",\"ultimatum\']'")
            if "first_opened_at" not in assignment_columns:
                connection.execute("ALTER TABLE experiment_assignments ADD COLUMN first_opened_at TEXT")
            if "last_opened_at" not in assignment_columns:
                connection.execute("ALTER TABLE experiment_assignments ADD COLUMN last_opened_at TEXT")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS experiment_consents ("
                "assignment_id TEXT NOT NULL REFERENCES experiment_assignments(assignment_id), "
                "consent_version TEXT NOT NULL, accepted_at TEXT NOT NULL, "
                "PRIMARY KEY(assignment_id, consent_version))"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS experiment_workflow_steps ("
                "participation_id TEXT NOT NULL REFERENCES study_participations(participation_id), "
                "step_index INTEGER NOT NULL, module_code TEXT NOT NULL CHECK(module_code IN ('questionnaire','dd_lifetrade','ultimatum')), "
                "status TEXT NOT NULL DEFAULT 'not_started' CHECK(status IN ('not_started','in_progress','completed')), "
                "attempt_number INTEGER NOT NULL DEFAULT 0, current_run_id TEXT REFERENCES instrument_runs(run_id), "
                "draft_payload TEXT NOT NULL DEFAULT '{}', started_at TEXT, completed_at TEXT, updated_at TEXT NOT NULL, "
                "PRIMARY KEY(participation_id,step_index), UNIQUE(current_run_id))"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS study_workflow_configs ("
                "study_code TEXT PRIMARY KEY REFERENCES studies(study_code), workflow_json TEXT NOT NULL, updated_at TEXT NOT NULL)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS experiment_activity_pings ("
                "ping_id TEXT PRIMARY KEY, assignment_id TEXT NOT NULL REFERENCES experiment_assignments(assignment_id), "
                "run_id TEXT REFERENCES instrument_runs(run_id), workflow_step_index INTEGER, active_seconds REAL NOT NULL CHECK(active_seconds BETWEEN 0 AND 60), "
                "received_at TEXT NOT NULL)"
            )
            activity_columns = {row["name"] for row in connection.execute("PRAGMA table_info(experiment_activity_pings)").fetchall()}
            if "workflow_step_index" not in activity_columns:
                connection.execute("ALTER TABLE experiment_activity_pings ADD COLUMN workflow_step_index INTEGER")
            connection.execute("CREATE INDEX IF NOT EXISTS idx_activity_pings_assignment ON experiment_activity_pings(assignment_id,received_at)")
            connection.execute("CREATE INDEX IF NOT EXISTS idx_activity_pings_run ON experiment_activity_pings(run_id,received_at)")
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
    """Idempotently register the study and all three planned experiment modules."""
    instrument_rows = [
        ("lifetrade", INSTRUMENT_NAME),
        ("questionnaire", "BarSan questionnaire"),
        ("ultimatum", "Ultimatum Game"),
    ]
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
    default_workflow = json.dumps(list(STAGE_ORDER), ensure_ascii=False)
    if DATABASE_URL:
        connection.execute(
            "INSERT INTO study_workflow_configs(study_code,workflow_json,updated_at) VALUES(%s,%s,%s) "
            "ON CONFLICT(study_code) DO NOTHING",
            (STUDY_CODE, default_workflow, now()),
        )
    else:
        connection.execute(
            "INSERT OR IGNORE INTO study_workflow_configs(study_code,workflow_json,updated_at) VALUES(?,?,?)",
            (STUDY_CODE, default_workflow, now()),
        )


init_db()


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

StageCode = Literal["questionnaire", "dd_lifetrade", "ultimatum"]


class AssignmentItemIn(BaseModel):
    participant_code: str = Field(min_length=1, max_length=100)
    condition_code: str = Field(min_length=1, max_length=64)


class AssignmentBatchIn(BaseModel):
    assignments: list[AssignmentItemIn] = Field(min_length=1, max_length=500)


class WorkflowConfigIn(BaseModel):
    workflow: list[StageCode] = Field(min_length=1, max_length=40)


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


class ModuleTestLinkIn(BaseModel):
    stage_code: StageCode
    condition_code: Literal["A", "B"] = "A"


class MaintenanceResetIn(BaseModel):
    scope: Literal["main", "test"]
    confirm_phrase: str = Field(min_length=1, max_length=40)


class ExperimentIdentityIn(BaseModel):
    invitation_token: str = Field(min_length=20, max_length=200)
    participant_code: str = Field(min_length=1, max_length=100)


class ExperimentConsentIn(ExperimentIdentityIn):
    accepted: bool


class ActivityOpenIn(BaseModel):
    invitation_token: str = Field(min_length=20, max_length=200)


class ActivityPingIn(ExperimentIdentityIn):
    ping_id: str = Field(min_length=16, max_length=100)
    run_id: str | None = Field(default=None, max_length=100)
    workflow_step_index: int | None = Field(default=None, ge=0, le=39)
    active_seconds: float = Field(ge=0, le=60)


class StageStartIn(ExperimentIdentityIn):
    stage_code: StageCode
    workflow_step_index: int | None = Field(default=None, ge=0, le=39)


class StageDraftIn(ExperimentIdentityIn):
    stage_code: Literal["questionnaire"] = "questionnaire"
    workflow_step_index: int | None = Field(default=None, ge=0, le=39)
    run_id: str = Field(min_length=1, max_length=100)
    draft_payload: dict[str, Any]


class QuestionnaireAnswerIn(ExperimentIdentityIn):
    workflow_step_index: int | None = Field(default=None, ge=0, le=39)
    run_id: str = Field(min_length=1, max_length=100)
    question_code: str = Field(min_length=1, max_length=16)
    response_value: Literal["1", "2", "3", "4", "5", "NA"]
    current_question_index: int = Field(default=0, ge=0, le=100)


class StageCompleteIn(ExperimentIdentityIn):
    stage_code: StageCode
    workflow_step_index: int | None = Field(default=None, ge=0, le=39)
    run_id: str = Field(min_length=1, max_length=100)
    final_payload: dict[str, Any] | None = None


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
    client_metadata: dict[str, Any] = Field(default_factory=dict)
    client_event_timestamps: dict[str, str] = Field(default_factory=dict)


class UltimatumEventsIn(ExperimentIdentityIn):
    run_id: str = Field(min_length=1, max_length=100)
    round_id: str = Field(min_length=1, max_length=100)
    event_timestamps: dict[str, str] = Field(min_length=1, max_length=10)


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




def _normalize_workflow(value: Any) -> list[str]:
    if not isinstance(value, list) or not value or len(value) > 40:
        raise HTTPException(422, "روند آزمایش باید حداقل یک و حداکثر ۴۰ ماژول داشته باشد.")
    allowed = set(STAGE_TO_INSTRUMENT)
    workflow: list[str] = []
    for item in value:
        module = str(item).strip()
        if module not in allowed:
            raise HTTPException(422, "یکی از ماژول‌های روند آزمایش معتبر نیست.")
        workflow.append(module)
    return workflow


def _current_study_workflow(connection: Any, study_code: str = STUDY_CODE) -> list[str]:
    marker = "%s" if DATABASE_URL else "?"
    row = connection.execute(
        "SELECT workflow_json FROM study_workflow_configs WHERE study_code=" + marker,
        (study_code,),
    ).fetchone()
    if not row:
        return list(STAGE_ORDER)
    raw = _json_value(row["workflow_json"])
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            raw = None
    try:
        return _normalize_workflow(raw)
    except HTTPException:
        return list(STAGE_ORDER)


def _assignment_workflow(assignment: dict[str, Any]) -> list[str]:
    raw = assignment.get("workflow_json")
    if not raw:
        return list(STAGE_ORDER)
    parsed = _json_value(raw)
    if isinstance(parsed, str):
        try:
            parsed = json.loads(parsed)
        except (TypeError, ValueError):
            parsed = None
    try:
        return _normalize_workflow(parsed)
    except HTTPException:
        return list(STAGE_ORDER)


def _ensure_workflow_progress(connection: Any, participation_id: str, workflow: list[str]) -> list[dict[str, Any]]:
    """Create per-position progress rows; map old single-module progress to its first occurrence."""
    marker = "%s" if DATABASE_URL else "?"
    existing = connection.execute(
        "SELECT step_index,module_code,status,attempt_number,current_run_id,draft_payload,started_at,completed_at,updated_at "
        "FROM experiment_workflow_steps WHERE participation_id=" + marker + " ORDER BY step_index",
        (participation_id,),
    ).fetchall()
    present = {int(row["step_index"]): dict(row) for row in existing}
    legacy_rows = _get_stage_rows(connection, participation_id)
    first_occurrence: set[str] = set()
    for index, module_code in enumerate(workflow):
        if index not in present:
            legacy = legacy_rows.get(module_code) if module_code not in first_occurrence else None
            values = (
                participation_id, index, module_code,
                legacy["status"] if legacy else "not_started",
                int(legacy["attempt_number"] or 0) if legacy else 0,
                legacy.get("current_run_id") if legacy else None,
                _json_param(_json_value(legacy.get("draft_payload")) if legacy else {}),
                legacy.get("started_at") if legacy else None,
                legacy.get("completed_at") if legacy else None,
                legacy.get("updated_at") if legacy else now(),
            )
            query = (
                "INSERT INTO experiment_workflow_steps(participation_id,step_index,module_code,status,attempt_number,"
                "current_run_id,draft_payload,started_at,completed_at,updated_at) VALUES("
                + ",".join([marker] * 10) + ") ON CONFLICT(participation_id,step_index) DO NOTHING"
            )
            connection.execute(query, values)
        first_occurrence.add(module_code)
    rows = connection.execute(
        "SELECT step_index,module_code,status,attempt_number,current_run_id,draft_payload,started_at,completed_at,updated_at "
        "FROM experiment_workflow_steps WHERE participation_id=" + marker + " ORDER BY step_index",
        (participation_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def _resolve_workflow_step(
    workflow: list[str], rows: list[dict[str, Any]], stage_code: str, requested_index: int | None
) -> tuple[int, dict[str, Any]]:
    if requested_index is not None:
        if requested_index >= len(workflow) or workflow[requested_index] != stage_code:
            raise HTTPException(409, "این جایگاه با ماژول درخواستی در روند آزمایش مطابقت ندارد.")
        index = requested_index
    else:
        index = next(
            (i for i, row in enumerate(rows) if row["module_code"] == stage_code and row["status"] != "completed"),
            -1,
        )
        if index < 0:
            raise HTTPException(409, "جایگاه تکمیل‌نشده‌ای برای این ماژول در روند آزمایش وجود ندارد.")
    row = next((item for item in rows if int(item["step_index"]) == index), None)
    if row is None:
        raise HTTPException(409, "جایگاه موردنظر در روند آزمایش پیدا نشد.")
    if any(item["status"] != "completed" for item in rows if int(item["step_index"]) < index):
        raise HTTPException(409, "ابتدا باید ماژول قبلی را تکمیل کنید.")
    return index, row


def _public_workflow_step(row: dict[str, Any]) -> dict[str, Any]:
    state = {
        "step_index": int(row["step_index"]),
        "stage_code": row["module_code"],
        "status": row["status"],
        "attempt_number": int(row["attempt_number"] or 0),
        "current_run_id": row["current_run_id"],
        "started_at": row["started_at"],
        "completed_at": row["completed_at"],
    }
    if row["module_code"] == "questionnaire":
        state["draft_payload"] = _json_value(row.get("draft_payload"))
    return state


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


def _lookup_assignment(connection: Any, invitation_token: str, participant_code: str) -> dict[str, Any]:
    token_hash = hashlib.sha256(invitation_token.encode("utf-8")).hexdigest()
    marker = "%s" if DATABASE_URL else "?"
    row = connection.execute(
        "SELECT a.assignment_id,a.study_code,a.participant_id,a.condition_code,a.workflow_json,"
        "sp.participation_id,p.participant_code "
        "FROM experiment_assignments a "
        "JOIN participants p ON p.participant_id=a.participant_id "
        "JOIN study_participations sp ON sp.participant_id=a.participant_id "
        "AND sp.study_code=a.study_code "
        "WHERE p.participant_code=" + marker
        + " AND a.invitation_token_hash=" + marker + " AND a.active=1",
        (normalize_participant_code(participant_code), token_hash),
    ).fetchone()
    if not row:
        # A generic error avoids distinguishing a bad code from a bad invitation link.
        raise HTTPException(403, "کد شرکت‌کننده یا لینک اختصاصی معتبر نیست.")
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
        workflow = _current_study_workflow(connection)
    return {
        "workflow": workflow,
        "modules": [
            {"code": "questionnaire", "label": "پرسشنامه", "description": "پرسشنامهٔ تجربهٔ همکاری"},
            {"code": "dd_lifetrade", "label": "DD / Life-Trade", "description": "تکلیف تصمیم‌گیری و ترجیح زمانی"},
            {"code": "ultimatum", "label": "بازی اولتیماتوم", "description": "بازی تقسیم امتیاز"},
        ],
        "max_steps": 40,
    }


@app.put("/api/admin/workflow")
def save_admin_workflow(
    data: WorkflowConfigIn,
    x_admin_token: str | None = Header(default=None),
):
    """Save the workflow template for future links; existing assignments keep their snapshots."""
    authorize(x_admin_token)
    workflow = _normalize_workflow(data.workflow)
    encoded = json.dumps(workflow, ensure_ascii=False, separators=(",", ":"))
    marker = "%s" if DATABASE_URL else "?"
    with conn() as connection:
        if DATABASE_URL:
            connection.execute(
                "INSERT INTO study_workflow_configs(study_code,workflow_json,updated_at) VALUES(%s,%s,%s) "
                "ON CONFLICT(study_code) DO UPDATE SET workflow_json=EXCLUDED.workflow_json,updated_at=EXCLUDED.updated_at",
                (STUDY_CODE, encoded, now()),
            )
        else:
            connection.execute(
                "INSERT INTO study_workflow_configs(study_code,workflow_json,updated_at) VALUES(?,?,?) "
                "ON CONFLICT(study_code) DO UPDATE SET workflow_json=excluded.workflow_json,updated_at=excluded.updated_at",
                (STUDY_CODE, encoded, now()),
            )
    return {"saved": True, "workflow": workflow, "future_links_only": True}


@app.post("/api/experiment/activity/open")
def record_assignment_open(data: ActivityOpenIn):
    """Record first/last landing time when the invitation URL is opened and identity is confirmed."""
    token_hash = hashlib.sha256(data.invitation_token.encode("utf-8")).hexdigest()
    marker = "%s" if DATABASE_URL else "?"
    timestamp = now()
    with conn() as connection:
        row = connection.execute(
            "SELECT assignment_id,participant_id FROM experiment_assignments WHERE invitation_token_hash="
            + marker + " AND active=1",
            (token_hash,),
        ).fetchone()
        if not row:
            raise HTTPException(403, "لینک اختصاصی معتبر نیست.")
        connection.execute(
            "UPDATE experiment_assignments SET first_opened_at=COALESCE(first_opened_at,"
            + marker + "),last_opened_at=" + marker + " WHERE assignment_id=" + marker,
            (timestamp, timestamp, row["assignment_id"]),
        )
    return {"recorded": True, "opened_at": timestamp}


@app.post("/api/experiment/activity/ping")
def record_activity_ping(data: ActivityPingIn):
    """Idempotently accumulate visible/active time for the participant and optional module run."""
    with conn() as connection:
        assignment = _lookup_assignment(connection, data.invitation_token, data.participant_code)
        marker = "%s" if DATABASE_URL else "?"
        step_index = data.workflow_step_index
        if data.run_id:
            step = connection.execute(
                "SELECT step_index FROM experiment_workflow_steps WHERE participation_id="
                + marker + " AND current_run_id=" + marker + " AND status='in_progress'",
                (assignment["participation_id"], data.run_id),
            ).fetchone()
            if not step:
                raise HTTPException(409, "اجرای ماژول برای ثبت زمان پیدا نشد.")
            if step_index is not None and int(step["step_index"]) != step_index:
                raise HTTPException(409, "جایگاه روند برای ثبت زمان با اجرای فعال مطابقت ندارد.")
            step_index = int(step["step_index"])
        if step_index is not None:
            workflow = _assignment_workflow(assignment)
            if step_index >= len(workflow):
                raise HTTPException(409, "جایگاه روند برای ثبت زمان معتبر نیست.")
        connection.execute(
            "INSERT INTO experiment_activity_pings(ping_id,assignment_id,run_id,workflow_step_index,active_seconds,received_at) "
            "VALUES(" + ",".join([marker] * 6) + ") ON CONFLICT(ping_id) DO NOTHING",
            (data.ping_id, assignment["assignment_id"], data.run_id, step_index,
             float(data.active_seconds), now()),
        )
        connection.execute(
            "UPDATE experiment_assignments SET first_opened_at=COALESCE(first_opened_at,"
            + marker + "),last_opened_at=" + marker + " WHERE assignment_id=" + marker,
            (now(), now(), assignment["assignment_id"]),
        )
    return {"recorded": True}


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
    with conn() as connection:
        workflow_json = json.dumps(_current_study_workflow(connection, STUDY_CODE), ensure_ascii=False, separators=(",", ":"))
        for item in data.assignments:
            code = normalize_participant_code(item.participant_code)
            condition = item.condition_code.strip().upper()
            if not condition:
                raise HTTPException(422, "Condition code is required")
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
                str(uuid.uuid4()), STUDY_CODE, participant_id, condition, token_hash, token_encrypted, workflow_json, 1, now()
            )
            if DATABASE_URL:
                connection.execute(
                    "INSERT INTO experiment_assignments(assignment_id,study_code,participant_id,condition_code,"
                    "invitation_token_hash,invitation_token_encrypted,workflow_json,active,assigned_at) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    values,
                )
            else:
                connection.execute(
                    "INSERT INTO experiment_assignments(assignment_id,study_code,participant_id,condition_code,"
                    "invitation_token_hash,invitation_token_encrypted,workflow_json,active,assigned_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    values,
                )
            created.append({
                "participant_code": code,
                "condition_code": condition,
                "invitation_url": base_url + joiner + "invite=" + invitation_token,
            })
    return {"created_count": len(created), "assignments": created}


@app.post("/api/admin/test-links")
def create_test_links(
    request: Request,
    x_admin_token: str | None = Header(default=None),
):
    """Create two isolated, fresh test studies with the shared participant code TEST."""
    authorize(x_admin_token)
    generation = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S") + "-" + secrets.token_hex(3)
    base_url = _invitation_base_url(request, "/api/admin/test-links")
    joiner = "&" if "?" in base_url else "?"
    created = []
    with conn() as connection:
        workflow_json = json.dumps(_current_study_workflow(connection, STUDY_CODE), ensure_ascii=False, separators=(",", ":"))
        for condition, group_label in (("A", "Frame"), ("B", "Control")):
            group_slug = group_label.lower()
            study_code = f"{TEST_STUDY_PREFIX}{generation}-{group_slug}"
            study_name = f"BarSan isolated test — {generation} — {group_label}"
            participant_code = "TEST"
            participant_id, _participation_id = _ensure_participation(
                connection,
                participant_code,
                study_code=study_code,
                study_name=study_name,
            )
            marker = "%s" if DATABASE_URL else "?"
            existing = connection.execute(
                "SELECT assignment_id FROM experiment_assignments "
                "WHERE study_code=" + marker + " AND participant_id=" + marker,
                (study_code, participant_id),
            ).fetchone()
            if existing:
                raise HTTPException(409, "A test assignment already exists for this test generation")
            invitation_token = secrets.token_urlsafe(32)
            token_hash = hashlib.sha256(invitation_token.encode("utf-8")).hexdigest()
            token_encrypted = _encrypt_invitation_token(invitation_token)
            values = (
                str(uuid.uuid4()), study_code, participant_id, condition, token_hash, token_encrypted, workflow_json, 1, now()
            )
            if DATABASE_URL:
                connection.execute(
                    "INSERT INTO experiment_assignments(assignment_id,study_code,participant_id,condition_code,"
                    "invitation_token_hash,invitation_token_encrypted,workflow_json,active,assigned_at) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    values,
                )
            else:
                connection.execute(
                    "INSERT INTO experiment_assignments(assignment_id,study_code,participant_id,condition_code,"
                    "invitation_token_hash,invitation_token_encrypted,workflow_json,active,assigned_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    values,
                )
            created.append({
                "participant_code": participant_code,
                "condition_code": condition,
                "group": group_label,
                "study_code": study_code,
                "invitation_url": base_url + joiner + "invite=" + invitation_token,
            })
    return {
        "test_code": "TEST",
        "generation": generation,
        "created_count": len(created),
        "assignments": created,
        "note": "Each click creates a new isolated test pair. Invitation URLs are encrypted at rest and can be exported later; preserve the private invitation-token key file when backing up this installation. Existing test data is retained.",
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
            "invitation_token_hash,invitation_token_encrypted,workflow_json,active,assigned_at) VALUES(" + ",".join([marker] * 9) + ")",
            (str(uuid.uuid4()), study_code, participant_id, data.condition_code, token_hash, token_encrypted,
             json.dumps([data.stage_code], ensure_ascii=False), 1, now()),
        )
    return {
        "participant_code": participant_code,
        "condition_code": data.condition_code,
        "group": "Frame" if data.condition_code == "A" else "Control",
        "stage_code": data.stage_code,
        "study_code": study_code,
        "invitation_url": base_url + joiner + "invite=" + invitation_token,
        "test_code": "TEST",
        "prerequisites_marked_synthetic": [],
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
        "SELECT a.assignment_id,a.study_code,p.participant_code,a.condition_code,"
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
        item["group"] = "Frame" if str(item["condition_code"]).upper() in {"A", "FRAME", "FRAMING"} else "Control"
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
    if len(set(data.assignment_ids)) != len(data.assignment_ids):
        raise HTTPException(422, "فهرست شناسه‌های لینک نباید تکراری باشد.")
    marker = "%s" if DATABASE_URL else "?"
    scope_condition, scope_params = _scope_filter(data.scope, "study_code", marker)
    changed = 0
    # Chunk large selections so SQLite deployments stay below their bind-parameter limit.
    with conn() as connection:
        for start in range(0, len(data.assignment_ids), 400):
            batch_ids = data.assignment_ids[start:start + 400]
            placeholders = ",".join([marker] * len(batch_ids))
            query = (
                "UPDATE experiment_assignments SET active=" + marker
                + " WHERE assignment_id IN (" + placeholders + ") AND " + scope_condition
            )
            params = (1 if data.active else 0, *batch_ids, *scope_params)
            cursor = connection.execute(query, params)
            changed += max(0, int(cursor.rowcount or 0))
    return {
        "updated_count": changed,
        "active": data.active,
        "scope": data.scope,
        "message": "وضعیت لینک‌ها تغییر کرد؛ داده‌های پژوهشی مرتبط دست‌نخورده ماندند.",
    }


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
            "SELECT a.assignment_id,a.study_code,p.participant_code,a.condition_code,a.active "
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
        "group": "Frame" if str(row["condition_code"]).upper() in {"A", "FRAME", "FRAMING"} else "Control",
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
    with conn() as connection:
        inactive_count = int(connection.execute(
            "SELECT COUNT(*) AS n FROM experiment_assignments WHERE active=0 AND " + scope_condition,
            scope_params,
        ).fetchone()["n"] or 0)
        if not inactive_count:
            return {
                "deleted_count": 0,
                "scope": data.scope,
                "message": "در محدودهٔ انتخاب‌شده لینک غیرفعالی برای حذف وجود ندارد.",
            }
        connection.execute(
            "DELETE FROM experiment_consents WHERE assignment_id IN "
            "(SELECT assignment_id FROM experiment_assignments WHERE active=0 AND " + scope_condition + ")",
            scope_params,
        )
        cursor = connection.execute(
            "DELETE FROM experiment_assignments WHERE active=0 AND " + scope_condition,
            scope_params,
        )
        deleted_count = max(0, int(cursor.rowcount or 0))
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
        "SELECT a.assignment_id,a.study_code,p.participant_code,a.condition_code,"
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
        "participant_code", "group", "condition_code", "active", "study_code",
        "assigned_at", "invitation_url", "url_available", "link_id",
    ]
    writer = csv.DictWriter(output, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    for raw in rows:
        item = dict(raw)
        token = _decrypt_invitation_token(item.get("invitation_token_encrypted"))
        writer.writerow({
            "participant_code": item["participant_code"],
            "group": "Frame" if str(item["condition_code"]).upper() in {"A", "FRAME", "FRAMING"} else "Control",
            "condition_code": item["condition_code"],
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


def _get_stage_rows_for_public(assignment: dict[str, Any], states: list[dict[str, Any]], stage_code: str):
    match = next((row for row in reversed(states) if row["stage_code"] == stage_code), None)
    return match


@app.post("/api/experiment/identify")
def identify_experiment_participant(data: ExperimentIdentityIn):
    """Resolve the assignment's frozen workflow and return per-position progress."""
    with conn() as connection:
        assignment = _lookup_assignment(connection, data.invitation_token, data.participant_code)
        _require_consent(connection, assignment["assignment_id"])
        workflow = _assignment_workflow(assignment)
        rows = _ensure_workflow_progress(connection, assignment["participation_id"], workflow)
    states = [_public_workflow_step(row) for row in rows]
    current = next((row for row in states if row["status"] != "completed"), None)
    return {
        "participant_code": assignment["participant_code"],
        "condition_code": assignment["condition_code"],
        "study_code": assignment["study_code"],
        "workflow": workflow,
        "workflow_steps": states,
        "current_step_index": current["step_index"] if current else None,
        "current_stage": current["stage_code"] if current else None,
        "test_module_only": _test_module_from_study_code(assignment["study_code"]),
        # Compatibility field for older participant clients.
        "stages": [_public_stage_state(stage, _get_stage_rows_for_public(assignment, states, stage)) for stage in dict.fromkeys(workflow)],
    }


@app.post("/api/experiment/stages/start")
def start_experiment_stage(data: StageStartIn):
    """Start or resume one occurrence of a module in the participant's assigned workflow."""
    with conn() as connection:
        assignment = _lookup_assignment(connection, data.invitation_token, data.participant_code)
        _require_consent(connection, assignment["assignment_id"])
        workflow = _assignment_workflow(assignment)
        rows = _ensure_workflow_progress(connection, assignment["participation_id"], workflow)
        step_index, existing = _resolve_workflow_step(
            workflow, rows, data.stage_code, data.workflow_step_index
        )

        if existing["status"] == "completed":
            return {
                "stage_code": data.stage_code,
                "workflow_step_index": step_index,
                "status": "completed",
                "already_completed": True,
                "run_id": existing["current_run_id"],
                "attempt_number": existing["attempt_number"],
            }

        if (
            data.stage_code == "questionnaire"
            and existing["status"] == "in_progress"
            and existing["current_run_id"]
        ):
            return {
                "stage_code": data.stage_code,
                "workflow_step_index": step_index,
                "status": "in_progress",
                "resumed": True,
                "already_completed": False,
                "run_id": existing["current_run_id"],
                "attempt_number": existing["attempt_number"],
                "draft_payload": _json_value(existing.get("draft_payload")),
                "condition_code": assignment["condition_code"],
            }

        instrument_code, version, instrument_name = STAGE_TO_INSTRUMENT[data.stage_code]
        attempt_number = int(existing["attempt_number"] or 0) + 1
        timestamp = now()
        old_run_id = existing.get("current_run_id")

        if old_run_id:
            marker = "%s" if DATABASE_URL else "?"
            connection.execute(
                "UPDATE instrument_runs SET status='superseded',completed_at=COALESCE(completed_at,"
                + marker + ") WHERE run_id=" + marker,
                (timestamp, old_run_id),
            )

        settings = {
            "module": data.stage_code,
            "study_code": assignment["study_code"],
            "workflow_step_index": step_index,
            "schema_version": 2,
        }
        marker = "%s" if DATABASE_URL else "?"
        if DATABASE_URL:
            connection.execute(
                "INSERT INTO instrument_versions(instrument_version_id,instrument_code,version,settings,created_at) "
                "VALUES(%s,%s,%s,%s,%s) ON CONFLICT(instrument_code,version) DO NOTHING",
                (str(uuid.uuid4()), instrument_code, version, Jsonb(settings), timestamp),
            )
            instrument_version = connection.execute(
                "SELECT instrument_version_id FROM instrument_versions WHERE instrument_code=%s AND version=%s",
                (instrument_code, version),
            ).fetchone()
        else:
            connection.execute(
                "INSERT OR IGNORE INTO instrument_versions(instrument_version_id,instrument_code,version,settings,created_at) "
                "VALUES(?,?,?,?,?)",
                (str(uuid.uuid4()), instrument_code, version, json.dumps(settings, ensure_ascii=False), timestamp),
            )
            instrument_version = connection.execute(
                "SELECT instrument_version_id FROM instrument_versions WHERE instrument_code=? AND version=?",
                (instrument_code, version),
            ).fetchone()

        run_id = str(uuid.uuid4())
        stage_draft: dict[str, Any] = {}
        if data.stage_code == "ultimatum":
            stage_draft["block_order"] = random.sample(
                list(ULTIMATUM_BLOCKS.keys()), k=ULTIMATUM_MAIN_BLOCK_COUNT
            )
        metadata = {
            "stage_code": data.stage_code,
            "workflow_step_index": step_index,
            "condition_code": assignment["condition_code"],
            "attempt_number": attempt_number,
            "block_order": stage_draft.get("block_order"),
        }
        if DATABASE_URL:
            connection.execute(
                "INSERT INTO instrument_runs(run_id,participation_id,instrument_version_id,started_at,status,metadata) "
                "VALUES(%s,%s,%s,%s,'in_progress',%s)",
                (run_id, assignment["participation_id"], instrument_version["instrument_version_id"],
                 timestamp, Jsonb(metadata)),
            )
        else:
            connection.execute(
                "INSERT INTO instrument_runs(run_id,participation_id,instrument_version_id,started_at,status,metadata) "
                "VALUES(?,?,?,?,?,?)",
                (run_id, assignment["participation_id"], instrument_version["instrument_version_id"],
                 timestamp, "in_progress", json.dumps(metadata, ensure_ascii=False)),
            )

        connection.execute(
            "UPDATE experiment_workflow_steps SET status='in_progress',attempt_number=" + marker
            + ",current_run_id=" + marker + ",draft_payload=" + marker + ",started_at=" + marker
            + ",completed_at=NULL,updated_at=" + marker
            + " WHERE participation_id=" + marker + " AND step_index=" + marker,
            (attempt_number, run_id, _json_param(stage_draft), timestamp, timestamp,
             assignment["participation_id"], step_index),
        )
        # Keep the former one-row-per-module table updated for compatibility with old exports/tools.
        connection.execute(
            "INSERT INTO experiment_stage_progress(participation_id,stage_code,status,attempt_number,current_run_id,"
            "draft_payload,started_at,completed_at,updated_at) VALUES("
            + ",".join([marker] * 9) + ") ON CONFLICT(participation_id,stage_code) DO UPDATE SET "
            "status='in_progress',attempt_number=excluded.attempt_number,current_run_id=excluded.current_run_id,"
            "draft_payload=excluded.draft_payload,started_at=excluded.started_at,completed_at=NULL,updated_at=excluded.updated_at",
            (assignment["participation_id"], data.stage_code, "in_progress", attempt_number, run_id,
             _json_param(stage_draft), timestamp, None, timestamp),
        )
    return {
        "stage_code": data.stage_code,
        "workflow_step_index": step_index,
        "status": "in_progress",
        "resumed": False,
        "already_completed": False,
        "run_id": run_id,
        "attempt_number": attempt_number,
        "draft_payload": stage_draft,
        "condition_code": assignment["condition_code"],
    }


@app.post("/api/experiment/stages/draft")
def save_questionnaire_draft(data: StageDraftIn):
    """Persist questionnaire answers continuously so a refresh restores the same draft."""
    with conn() as connection:
        assignment = _lookup_assignment(connection, data.invitation_token, data.participant_code)
        _require_consent(connection, assignment["assignment_id"])
        marker = "%s" if DATABASE_URL else "?"
        state = connection.execute(
            "SELECT status,current_run_id FROM experiment_workflow_steps "
            "WHERE participation_id=" + marker + " AND current_run_id=" + marker + " AND module_code='questionnaire'",
            (assignment["participation_id"], data.run_id),
        ).fetchone()
        if not state or state["current_run_id"] != data.run_id:
            raise HTTPException(409, "این اجرای پرسشنامه دیگر فعال نیست.")
        if state["status"] == "completed":
            raise HTTPException(409, "پرسشنامه قبلاً تکمیل شده است.")
        connection.execute(
            "UPDATE experiment_workflow_steps SET draft_payload=" + marker + ",updated_at=" + marker
            + " WHERE participation_id=" + marker + " AND current_run_id=" + marker,
            (_json_param(data.draft_payload), now(), assignment["participation_id"], data.run_id),
        )
        connection.execute(
            "UPDATE experiment_stage_progress SET draft_payload=" + marker + ",updated_at=" + marker
            + " WHERE participation_id=" + marker + " AND stage_code='questionnaire'",
            (_json_param(data.draft_payload), now(), assignment["participation_id"]),
        )
    return {"saved": True, "stage_code": data.stage_code, "run_id": data.run_id}


@app.post("/api/experiment/questionnaire/answer")
def save_questionnaire_answer(data: QuestionnaireAnswerIn):
    """Upsert one answer by its hidden question code and refresh the server-side draft."""
    if data.question_code not in QUESTIONNAIRE_QUESTION_CODES:
        raise HTTPException(422, "Unknown questionnaire question code")
    with conn() as connection:
        assignment = _lookup_assignment(connection, data.invitation_token, data.participant_code)
        _require_consent(connection, assignment["assignment_id"])
        marker = "%s" if DATABASE_URL else "?"
        state = connection.execute(
            "SELECT status,current_run_id,draft_payload FROM experiment_workflow_steps "
            "WHERE participation_id=" + marker + " AND current_run_id=" + marker + " AND module_code='questionnaire'",
            (assignment["participation_id"], data.run_id),
        ).fetchone()
        if not state or state["current_run_id"] != data.run_id:
            raise HTTPException(409, "این اجرای پرسشنامه دیگر فعال نیست.")
        if state["status"] != "in_progress":
            raise HTTPException(409, "پرسشنامه در وضعیت قابل ویرایش نیست.")
        timestamp = now()
        if DATABASE_URL:
            connection.execute(
                "INSERT INTO questionnaire_responses(run_id,question_code,response_value,updated_at) "
                "VALUES(%s,%s,%s,%s) ON CONFLICT(run_id,question_code) DO UPDATE SET "
                "response_value=EXCLUDED.response_value,updated_at=EXCLUDED.updated_at",
                (data.run_id, data.question_code, data.response_value, timestamp),
            )
        else:
            connection.execute(
                "INSERT INTO questionnaire_responses(run_id,question_code,response_value,updated_at) "
                "VALUES(?,?,?,?) ON CONFLICT(run_id,question_code) DO UPDATE SET "
                "response_value=excluded.response_value,updated_at=excluded.updated_at",
                (data.run_id, data.question_code, data.response_value, timestamp),
            )
        draft = _json_value(state["draft_payload"])
        answers = draft.get("answers")
        if not isinstance(answers, dict):
            answers = {}
        answers[data.question_code] = data.response_value
        draft["answers"] = answers
        draft["current_question_index"] = data.current_question_index
        connection.execute(
            "UPDATE experiment_workflow_steps SET draft_payload=" + marker + ",updated_at=" + marker
            + " WHERE participation_id=" + marker + " AND current_run_id=" + marker,
            (_json_param(draft), timestamp, assignment["participation_id"], data.run_id),
        )
        connection.execute(
            "UPDATE experiment_stage_progress SET draft_payload=" + marker + ",updated_at=" + marker
            + " WHERE participation_id=" + marker + " AND stage_code='questionnaire'",
            (_json_param(draft), timestamp, assignment["participation_id"]),
        )
    return {
        "saved": True,
        "question_code": data.question_code,
        "response_value": data.response_value,
        "current_question_index": data.current_question_index,
    }


def _active_ultimatum_run(connection: Any, participation_id: str, run_id: str) -> None:
    marker = "%s" if DATABASE_URL else "?"
    state = connection.execute(
        "SELECT status,current_run_id FROM experiment_workflow_steps "
        "WHERE participation_id=" + marker + " AND current_run_id=" + marker + " AND module_code='ultimatum'",
        (participation_id, run_id),
    ).fetchone()
    if not state or state["status"] != "in_progress" or state["current_run_id"] != run_id:
        raise HTTPException(409, "این اجرای اولتیماتوم دیگر فعال نیست.")
    instrument = connection.execute(
        "SELECT iv.instrument_code FROM instrument_runs ir "
        "JOIN instrument_versions iv ON iv.instrument_version_id=ir.instrument_version_id "
        "WHERE ir.run_id=" + marker,
        (run_id,),
    ).fetchone()
    if not instrument or instrument["instrument_code"] != "ultimatum":
        raise HTTPException(409, "شناسهٔ اجرای اولتیماتوم معتبر نیست.")


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
        assignment = _lookup_assignment(connection, data.invitation_token, data.participant_code)
        _require_consent(connection, assignment["assignment_id"])
        _active_ultimatum_run(connection, assignment["participation_id"], data.run_id)
        marker = "%s" if DATABASE_URL else "?"
        stage_state = connection.execute(
            "SELECT draft_payload FROM experiment_workflow_steps "
            "WHERE participation_id=" + marker + " AND module_code='ultimatum' "
            "AND current_run_id=" + marker + " AND status='in_progress'",
            (assignment["participation_id"], data.run_id),
        ).fetchone()
        if not stage_state:
            raise HTTPException(409, "اجرای فعال اولتیماتوم پیدا نشد.")
        run_draft = _json_value(stage_state["draft_payload"])
        server_block_order = run_draft.get("block_order")

        practice_rows = connection.execute(
            "SELECT block_code,status FROM ultimatum_trials "
            "WHERE run_id=" + marker + " AND phase='practice' "
            "ORDER BY started_at,round_id",
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
            completed_main = connection.execute(
                "SELECT COUNT(*) AS n FROM ultimatum_trials "
                "WHERE run_id=" + marker + " AND phase='main' AND status='completed'",
                (data.run_id,),
            ).fetchone()
            if data.trial_sequence != int(completed_main["n"]) + 1:
                raise HTTPException(409, "دورهای اصلی باید به‌ترتیب و بدون پرش اجرا شوند.")

        if data.phase == "main":
            same_order = connection.execute(
                "SELECT DISTINCT block_code FROM ultimatum_trials "
                "WHERE run_id=" + ("%s" if DATABASE_URL else "?") + " AND phase='main' AND block_order="
                + ("%s" if DATABASE_URL else "?"),
                (data.run_id, data.block_order),
            ).fetchall()
            if any(row["block_code"] != data.block_code for row in same_order):
                raise HTTPException(409, "ترتیب بلوک‌ها در این اجرای اولتیماتوم تغییر کرده است.")
            other_order = connection.execute(
                "SELECT DISTINCT block_order FROM ultimatum_trials "
                "WHERE run_id=" + ("%s" if DATABASE_URL else "?") + " AND phase='main' AND block_code="
                + ("%s" if DATABASE_URL else "?"),
                (data.run_id, data.block_code),
            ).fetchall()
            if any(int(row["block_order"]) != data.block_order for row in other_order):
                raise HTTPException(409, "هر بلوک اولتیماتوم باید فقط یک‌بار اجرا شود.")
        marker = "%s" if DATABASE_URL else "?"
        existing = connection.execute(
            "SELECT * FROM ultimatum_trials WHERE run_id=" + marker
            + " AND phase=" + marker + " AND block_code=" + marker + " AND trial_in_block=" + marker,
            (data.run_id, data.phase, data.block_code, data.trial_in_block),
        ).fetchone()
        if existing:
            if existing["status"] != "prepared":
                raise HTTPException(409, "این دور اولتیماتوم قبلاً تکمیل شده است.")
            row = dict(existing)
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


@app.post("/api/experiment/ultimatum/resolve")
def resolve_ultimatum_round(data: UltimatumResolveIn):
    """Resolve participant choice, generate AI responses server-side, and persist trial-level data."""
    _validate_event_timestamps(data.client_event_timestamps, ULTIMATUM_PRE_RESOLVE_EVENT_FIELDS)
    with conn() as connection:
        assignment = _lookup_assignment(connection, data.invitation_token, data.participant_code)
        _require_consent(connection, assignment["assignment_id"])
        _active_ultimatum_run(connection, assignment["participation_id"], data.run_id)
        marker = "%s" if DATABASE_URL else "?"
        row = connection.execute(
            "SELECT * FROM ultimatum_trials WHERE round_id=" + marker + " AND run_id=" + marker,
            (data.round_id, data.run_id),
        ).fetchone()
        if not row:
            raise HTTPException(404, "دور اولتیماتوم پیدا نشد.")
        record = dict(row)

        def stored_resolve_result(completed_record: dict[str, Any]) -> dict[str, Any]:
            saved_payload = _json_value(completed_record["payload"])
            existing_result = saved_payload.get("resolve_result")
            if isinstance(existing_result, dict) and existing_result.get("round_id"):
                return existing_result
            total = connection.execute(
                "SELECT COALESCE(SUM(points_earned),0) AS score,"
                "COALESCE(SUM(potential_points_lost),0) AS lost "
                "FROM ultimatum_trials WHERE run_id=" + marker + " AND phase='main' AND status='completed'",
                (data.run_id,),
            ).fetchone()
            block_total = connection.execute(
                "SELECT COALESCE(SUM(points_earned),0) AS score,"
                "COALESCE(SUM(potential_points_lost),0) AS lost "
                "FROM ultimatum_trials WHERE run_id=" + marker + " AND phase='main' AND status='completed' "
                "AND block_code=" + marker,
                (data.run_id, completed_record["block_code"]),
            ).fetchone()
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
        total = connection.execute(
            "SELECT COALESCE(SUM(points_earned),0) AS score,"
            "COALESCE(SUM(potential_points_lost),0) AS lost "
            "FROM ultimatum_trials WHERE run_id=" + marker + " AND phase='main' AND status='completed'",
            (data.run_id,),
        ).fetchone()
        block_total = connection.execute(
            "SELECT COALESCE(SUM(points_earned),0) AS score,"
            "COALESCE(SUM(potential_points_lost),0) AS lost "
            "FROM ultimatum_trials WHERE run_id=" + marker + " AND phase='main' AND status='completed' "
            "AND block_code=" + marker,
            (data.run_id, record["block_code"]),
        ).fetchone()
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


@app.post("/api/experiment/ultimatum/events")
def record_ultimatum_events(data: UltimatumEventsIn):
    """Append post-decision client event times to a completed, active-attempt trial."""
    _validate_event_timestamps(data.event_timestamps, ULTIMATUM_POST_RESOLVE_EVENT_FIELDS)
    marker = "%s" if DATABASE_URL else "?"
    with conn() as connection:
        assignment = _lookup_assignment(connection, data.invitation_token, data.participant_code)
        _require_consent(connection, assignment["assignment_id"])
        _active_ultimatum_run(connection, assignment["participation_id"], data.run_id)
        row = connection.execute(
            "SELECT status,payload FROM ultimatum_trials WHERE round_id=" + marker + " AND run_id=" + marker,
            (data.round_id, data.run_id),
        ).fetchone()
        if not row:
            raise HTTPException(404, "دور اولتیماتوم پیدا نشد.")
        if row["status"] != "completed":
            raise HTTPException(409, "رخدادهای پس از نتیجه فقط برای دور تکمیل‌شده ثبت می‌شوند.")
        payload = _json_value(row["payload"])
        timestamps = payload.get("client_event_timestamps") or {}
        # Keep first-write timestamps so retries remain idempotent and auditable.
        for event_name, event_time in data.event_timestamps.items():
            timestamps.setdefault(event_name, event_time)
        payload["client_event_timestamps"] = timestamps
        payload["client_event_server_received_at"] = now()
        connection.execute(
            "UPDATE ultimatum_trials SET payload=" + marker + " WHERE round_id=" + marker + " AND run_id=" + marker,
            (_json_param(payload), data.round_id, data.run_id),
        )
    return {"round_id": data.round_id, "saved": True, "event_timestamps": timestamps}


@app.post("/api/experiment/stages/complete")
def complete_experiment_stage(data: StageCompleteIn):
    """Mark a module complete only for its currently active attempt."""
    with conn() as connection:
        assignment = _lookup_assignment(connection, data.invitation_token, data.participant_code)
        _require_consent(connection, assignment["assignment_id"])
        marker = "%s" if DATABASE_URL else "?"
        state = connection.execute(
            "SELECT status,current_run_id,attempt_number,draft_payload,step_index,module_code FROM experiment_workflow_steps "
            "WHERE participation_id=" + marker + " AND current_run_id=" + marker + " AND module_code=" + marker,
            (assignment["participation_id"], data.run_id, data.stage_code),
        ).fetchone()
        if not state:
            raise HTTPException(409, "این مرحله هنوز شروع نشده است.")
        if state["current_run_id"] != data.run_id:
            raise HTTPException(409, "این اجرای مرحله دیگر فعال نیست.")
        if state["status"] == "completed":
            return {"completed": True, "already_completed": True, "stage_code": data.stage_code}
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
        # Per-occurrence progress is authoritative; the legacy stage table remains a compatibility summary.
        if data.stage_code in {"questionnaire", "ultimatum"} and data.final_payload is not None:
            workflow_payload = _json_param(data.final_payload)
            connection.execute(
                "UPDATE experiment_workflow_steps SET status='completed',draft_payload=" + marker
                + ",completed_at=" + marker + ",updated_at=" + marker
                + " WHERE participation_id=" + marker + " AND current_run_id=" + marker,
                (workflow_payload, timestamp, timestamp, assignment["participation_id"], data.run_id),
            )
        else:
            connection.execute(
                "UPDATE experiment_workflow_steps SET status='completed',completed_at=" + marker
                + ",updated_at=" + marker
                + " WHERE participation_id=" + marker + " AND current_run_id=" + marker,
                (timestamp, timestamp, assignment["participation_id"], data.run_id),
            )
        connection.execute(
            "UPDATE instrument_runs SET status='completed',completed_at=" + marker + " WHERE run_id=" + marker,
            (timestamp, data.run_id),
        )
    return {"completed": True, "already_completed": False, "stage_code": data.stage_code,
            "workflow_step_index": state["step_index"]}



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
                "SELECT 1 FROM instrument_runs ir "
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
                "SELECT 1 FROM instrument_runs ir "
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
        "opened_assignments": ("SELECT COUNT(*) AS n FROM experiment_assignments WHERE " + condition + " AND first_opened_at IS NOT NULL", params),
        "workflow_steps": (
            "SELECT COUNT(*) AS n FROM experiment_workflow_steps s JOIN study_participations sp "
            "ON sp.participation_id=s.participation_id WHERE " + condition.replace("study_code", "sp.study_code"), params,
        ),
        "activity_pings": (
            "SELECT COUNT(*) AS n FROM experiment_activity_pings p JOIN experiment_assignments a "
            "ON a.assignment_id=p.assignment_id WHERE " + condition.replace("study_code", "a.study_code"), params,
        ),
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
        condition_sp = condition.replace("study_code", "sp.study_code")
        counts["completed_experiments"] = int(connection.execute(
            "SELECT COUNT(*) AS n FROM (SELECT sp.participation_id "
            "FROM study_participations sp JOIN experiment_stage_progress q "
            "ON q.participation_id=sp.participation_id AND q.stage_code='questionnaire' AND q.status='completed' "
            "JOIN experiment_stage_progress d ON d.participation_id=sp.participation_id "
            "AND d.stage_code='dd_lifetrade' AND d.status='completed' "
            "JOIN experiment_stage_progress u ON u.participation_id=sp.participation_id "
            "AND u.stage_code='ultimatum' AND u.status='completed' WHERE " + condition_sp + ") completed",
            params,
        ).fetchone()["n"] or 0)
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
            ("experiment_workflow_steps",
             "DELETE FROM experiment_workflow_steps WHERE participation_id IN "
             "(SELECT sp.participation_id FROM study_participations sp WHERE "
             + condition.replace("study_code", "sp.study_code") + ")"),
            ("experiment_activity_pings",
             "DELETE FROM experiment_activity_pings WHERE assignment_id IN "
             "(SELECT a.assignment_id FROM experiment_assignments a WHERE "
             + condition.replace("study_code", "a.study_code") + ")"),
            ("experiment_consents",
             "DELETE FROM experiment_consents WHERE assignment_id IN "
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
        if data.scope == "test":
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
):
    """Return assignment, configured workflow, per-step progress and active-time measurements."""
    authorize(x_admin_token)
    marker = "%s" if DATABASE_URL else "?"
    condition, params = _scope_filter(scope, "a.study_code", marker)
    query = (
        "SELECT a.assignment_id,a.study_code,a.workflow_json,a.assigned_at,a.first_opened_at,a.last_opened_at,"
        "p.participant_code,a.condition_code,sp.participation_id "
        "FROM experiment_assignments a "
        "JOIN participants p ON p.participant_id=a.participant_id "
        "LEFT JOIN study_participations sp ON sp.participant_id=a.participant_id AND sp.study_code=a.study_code "
        "WHERE " + condition + " AND a.active=1 ORDER BY a.assigned_at,p.participant_code"
    )
    with conn() as connection:
        assignments = [dict(row) for row in connection.execute(query, params).fetchall()]
        # Seed per-position rows lazily for existing assignments using the immutable link workflow.
        for assignment in assignments:
            if assignment.get("participation_id"):
                workflow = _assignment_workflow(assignment)
                _ensure_workflow_progress(connection, assignment["participation_id"], workflow)

        step_rows: list[dict[str, Any]] = []
        assignment_ids = [row["assignment_id"] for row in assignments]
        for start_index in range(0, len(assignment_ids), 400):
            batch = assignment_ids[start_index:start_index + 400]
            if not batch:
                continue
            placeholders = ",".join([marker] * len(batch))
            step_rows.extend(dict(row) for row in connection.execute(
                "SELECT s.participation_id,s.step_index,s.module_code,s.status,s.attempt_number,"
                "s.current_run_id,s.started_at,s.completed_at,s.updated_at "
                "FROM experiment_workflow_steps s "
                "JOIN study_participations sp ON sp.participation_id=s.participation_id "
                "JOIN experiment_assignments a ON a.participant_id=sp.participant_id AND a.study_code=sp.study_code "
                "WHERE a.assignment_id IN (" + placeholders + ") ORDER BY a.assignment_id,s.step_index",
                tuple(batch),
            ).fetchall())

        activity_rows: list[dict[str, Any]] = []
        for start_index in range(0, len(assignment_ids), 400):
            batch = assignment_ids[start_index:start_index + 400]
            if not batch:
                continue
            placeholders = ",".join([marker] * len(batch))
            activity_rows.extend(dict(row) for row in connection.execute(
                "SELECT assignment_id,workflow_step_index,SUM(active_seconds) AS active_seconds,MAX(received_at) AS last_ping_at "
                "FROM experiment_activity_pings WHERE assignment_id IN (" + placeholders + ") "
                "GROUP BY assignment_id,workflow_step_index",
                tuple(batch),
            ).fetchall())

    current_time = datetime.now(timezone.utc)
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

    steps_by_participation: dict[str, list[dict[str, Any]]] = {}
    for item in step_rows:
        steps_by_participation.setdefault(item["participation_id"], []).append(item)

    activity_by_assignment: dict[str, dict[str, Any]] = {}
    for item in activity_rows:
        summary = activity_by_assignment.setdefault(item["assignment_id"], {
            "total_active_seconds": 0.0, "per_step": {}, "last_ping_at": None,
        })
        seconds = max(0.0, float(item["active_seconds"] or 0))
        summary["total_active_seconds"] += seconds
        if item.get("workflow_step_index") is not None:
            step_index = int(item["workflow_step_index"])
            summary["per_step"][step_index] = summary["per_step"].get(step_index, 0.0) + seconds
        ping_epoch = parse_time(item.get("last_ping_at"))
        existing_epoch = parse_time(summary["last_ping_at"])
        if ping_epoch is not None and (existing_epoch is None or ping_epoch > existing_epoch):
            summary["last_ping_at"] = item["last_ping_at"]

    output = []
    for assignment in assignments:
        row = dict(assignment)
        workflow = _assignment_workflow(row)
        participation_id = row.get("participation_id")
        raw_steps = steps_by_participation.get(participation_id, [])
        activity = activity_by_assignment.get(row["assignment_id"], {
            "total_active_seconds": 0.0, "per_step": {}, "last_ping_at": None,
        })
        steps = []
        module_times: dict[str, float] = {}
        last_activity_candidates = [
            parse_time(row.get("assigned_at")),
            parse_time(row.get("first_opened_at")),
            parse_time(row.get("last_opened_at")),
            parse_time(activity.get("last_ping_at")),
        ]
        for raw in raw_steps:
            seconds = float(activity["per_step"].get(int(raw["step_index"]), 0.0))
            item = {
                "step_index": int(raw["step_index"]),
                "stage_code": raw["module_code"],
                "status": raw["status"],
                "attempt_number": int(raw["attempt_number"] or 0),
                "current_run_id": raw["current_run_id"],
                "started_at": raw["started_at"],
                "completed_at": raw["completed_at"],
                "updated_at": raw["updated_at"],
                "active_seconds": round(seconds, 1),
            }
            steps.append(item)
            module_times[item["stage_code"]] = module_times.get(item["stage_code"], 0.0) + seconds
            last_activity_candidates.extend([
                parse_time(raw.get("started_at")),
                parse_time(raw.get("completed_at")),
                parse_time(raw.get("updated_at")),
            ])
        completed_steps = sum(step["status"] == "completed" for step in steps)
        current_step = next((step for step in steps if step["status"] != "completed"), None)
        row["workflow"] = workflow
        row["workflow_steps"] = steps
        row["total_steps"] = len(workflow)
        row["completed_steps"] = completed_steps
        row["completed_stages"] = completed_steps  # retained for existing UI / clients
        row["current_step_index"] = current_step["step_index"] if current_step else None
        row["current_stage"] = current_step["stage_code"] if current_step else None
        row["module_times"] = [
            {"stage_code": module, "active_seconds": round(module_times.get(module, 0.0), 1)}
            for module in dict.fromkeys(workflow)
        ]
        row["total_active_seconds"] = round(float(activity["total_active_seconds"]), 1)
        first_open_epoch = parse_time(row.get("first_opened_at"))
        row["elapsed_seconds_since_first_open"] = (
            round(max(0.0, current_time.timestamp() - first_open_epoch), 1)
            if first_open_epoch is not None else None
        )
        valid_times = [value for value in last_activity_candidates if value is not None]
        last_activity_epoch = max(valid_times) if valid_times else None
        row["last_activity_at"] = (
            datetime.fromtimestamp(last_activity_epoch, timezone.utc).isoformat()
            if last_activity_epoch is not None else row.get("assigned_at")
        )
        row["hours_since_activity"] = (
            round(max(0.0, (current_time.timestamp() - last_activity_epoch) / 3600), 2)
            if last_activity_epoch is not None else None
        )
        row["progress_state"] = (
            "completed" if completed_steps == len(workflow)
            else "not_started" if not any(int(step["attempt_number"]) > 0 for step in steps)
            else "stale_incomplete" if last_activity_epoch is not None and last_activity_epoch < stale_cutoff
            else "in_progress"
        )
        # Backward-compatible fields for the former fixed three-module dashboard.
        for module in STAGE_ORDER:
            matching = [step for step in steps if step["stage_code"] == module]
            latest = matching[-1] if matching else None
            row[module + "_status"] = latest["status"] if latest else "not_in_workflow"
            row[module + "_attempts"] = sum(step["attempt_number"] for step in matching)
            row[module + "_started_at"] = latest["started_at"] if latest else None
            row[module + "_completed_at"] = latest["completed_at"] if latest else None
            row[module + "_updated_at"] = latest["updated_at"] if latest else None
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
        "opponent_condition", "opponent_actual_type", "round_status", "participant_share",
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
            item["feedback_group"] = (
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


@app.get("/api/admin/export-all.zip")
async def export_all_modules_zip(
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
    ]
    archive_buffer = io.BytesIO()
    with zipfile.ZipFile(archive_buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
        for filename, response in responses:
            archive.writestr(filename, await _read_streaming_response(response))
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
        return file.read()


@app.get("/admin", response_class=HTMLResponse)
@app.get("/admin/links", response_class=HTMLResponse)
def admin_links_page():
    """Serve the protected-action UI; actual link creation/export still requires ADMIN_TOKEN."""
    path = os.path.join(ROOT, "app", "admin-links.html")
    try:
        with open(path, encoding="utf-8") as file:
            return HTMLResponse(file.read())
    except OSError as exc:
        raise HTTPException(503, "Admin link page is not installed on this server.") from exc


@app.get("/", response_class=HTMLResponse)
def home():
    return load_homepage()
