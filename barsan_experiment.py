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
from contextlib import contextmanager
from datetime import datetime, timezone
from functools import lru_cache
from typing import Any, Iterator, Literal

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

ROOT = os.path.dirname(os.path.abspath(__file__))
DATABASE_URL = os.getenv("DATABASE_URL", "").replace("postgres://", "postgresql://", 1)
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "")
STUDY_CODE = os.getenv("STUDY_CODE", "main-study").strip() or "main-study"
STUDY_NAME = os.getenv("STUDY_NAME", "BarSan Experiment").strip() or "BarSan Experiment"
PUBLIC_EXPERIMENT_URL = os.getenv("PUBLIC_EXPERIMENT_URL", "https://my-experiment.ir/lifetrade").rstrip("?&")
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


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_participant_code(value: str) -> str:
    # Existing participant codes are supplied by the research team/participant;
    # this function normalizes input only and never generates a participant code.
    normalized = value.strip().upper()
    if not normalized:
        raise HTTPException(422, "Participant code is required")
    return normalized


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
            "application_name": "lifetrade-api",
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
                "active INTEGER NOT NULL DEFAULT 1, assigned_at TEXT NOT NULL, "
                "UNIQUE(study_code, participant_id))"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS experiment_consents ("
                "assignment_id TEXT NOT NULL REFERENCES experiment_assignments(assignment_id), "
                "consent_version TEXT NOT NULL, accepted_at TEXT NOT NULL, "
                "PRIMARY KEY(assignment_id, consent_version))"
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
                "active INTEGER NOT NULL DEFAULT 1, assigned_at TEXT NOT NULL, "
                "UNIQUE(study_code, participant_id))"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS experiment_consents ("
                "assignment_id TEXT NOT NULL REFERENCES experiment_assignments(assignment_id), "
                "consent_version TEXT NOT NULL, accepted_at TEXT NOT NULL, "
                "PRIMARY KEY(assignment_id, consent_version))"
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


class ExperimentIdentityIn(BaseModel):
    invitation_token: str = Field(min_length=20, max_length=200)
    participant_code: str = Field(min_length=1, max_length=100)


class ExperimentConsentIn(ExperimentIdentityIn):
    accepted: bool


class StageStartIn(ExperimentIdentityIn):
    stage_code: StageCode


class StageDraftIn(ExperimentIdentityIn):
    stage_code: Literal["questionnaire"] = "questionnaire"
    run_id: str = Field(min_length=1, max_length=100)
    draft_payload: dict[str, Any]


class QuestionnaireAnswerIn(ExperimentIdentityIn):
    run_id: str = Field(min_length=1, max_length=100)
    question_code: str = Field(min_length=1, max_length=16)
    response_value: Literal["1", "2", "3", "4", "5", "NA"]
    current_question_index: int = Field(default=0, ge=0, le=100)


class StageCompleteIn(ExperimentIdentityIn):
    stage_code: StageCode
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
            "service": "lifetrade",
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
                "service": "lifetrade",
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


def _ensure_participation(connection: Any, participant_code: str) -> tuple[str, str]:
    """Create/reuse a participant and study participation without making a new code."""
    participant_id = str(uuid.uuid4())
    participation_id = str(uuid.uuid4())
    if DATABASE_URL:
        connection.execute(
            "INSERT INTO participants(participant_id,participant_code,created_at) VALUES(%s,%s,%s) "
            "ON CONFLICT(participant_code) DO NOTHING",
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
            "SELECT participation_id FROM study_participations WHERE study_code=%s AND participant_id=%s",
            (STUDY_CODE, participant["participant_id"]),
        ).fetchone()
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
    return participant["participant_id"], participation["participation_id"]


def _lookup_assignment(connection: Any, invitation_token: str, participant_code: str) -> dict[str, Any]:
    token_hash = hashlib.sha256(invitation_token.encode("utf-8")).hexdigest()
    marker = "%s" if DATABASE_URL else "?"
    row = connection.execute(
        "SELECT a.assignment_id,a.study_code,a.participant_id,a.condition_code,"
        "sp.participation_id,p.participant_code "
        "FROM experiment_assignments a "
        "JOIN participants p ON p.participant_id=a.participant_id "
        "JOIN study_participations sp ON sp.participant_id=a.participant_id "
        "AND sp.study_code=a.study_code "
        "WHERE a.study_code=" + marker + " AND p.participant_code=" + marker
        + " AND a.invitation_token_hash=" + marker + " AND a.active=1",
        (STUDY_CODE, normalize_participant_code(participant_code), token_hash),
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


@app.post("/api/admin/assignments")
def create_assignments(data: AssignmentBatchIn, x_admin_token: str | None = Header(default=None)):
    """Create fixed per-participant assignments and one-time-displayed invite links."""
    authorize(x_admin_token)
    created = []
    seen_codes = set()
    base_url = PUBLIC_EXPERIMENT_URL
    joiner = "&" if "?" in base_url else "?"
    with conn() as connection:
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
            values = (
                str(uuid.uuid4()), STUDY_CODE, participant_id, condition, token_hash, 1, now()
            )
            if DATABASE_URL:
                connection.execute(
                    "INSERT INTO experiment_assignments(assignment_id,study_code,participant_id,condition_code,"
                    "invitation_token_hash,active,assigned_at) VALUES(%s,%s,%s,%s,%s,%s,%s)",
                    values,
                )
            else:
                connection.execute(
                    "INSERT INTO experiment_assignments(assignment_id,study_code,participant_id,condition_code,"
                    "invitation_token_hash,active,assigned_at) VALUES(?,?,?,?,?,?,?)",
                    values,
                )
            created.append({
                "participant_code": code,
                "condition_code": condition,
                "invitation_url": base_url + joiner + "invite=" + invitation_token,
            })
    return {"created_count": len(created), "assignments": created}


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


@app.post("/api/experiment/identify")
def identify_experiment_participant(data: ExperimentIdentityIn):
    """Resolve the preassigned link/code and return authoritative saved stage progress."""
    with conn() as connection:
        assignment = _lookup_assignment(connection, data.invitation_token, data.participant_code)
        _require_consent(connection, assignment["assignment_id"])
        rows = _get_stage_rows(connection, assignment["participation_id"])
    states = [_public_stage_state(stage, rows.get(stage)) for stage in STAGE_ORDER]
    current_stage = next((state["stage_code"] for state in states if state["status"] != "completed"), None)
    return {
        "participant_code": assignment["participant_code"],
        "condition_code": assignment["condition_code"],
        "current_stage": current_stage,
        "stages": states,
    }


@app.post("/api/experiment/stages/start")
def start_experiment_stage(data: StageStartIn):
    """Resume questionnaire draft, or supersede/restart an incomplete task attempt."""
    with conn() as connection:
        assignment = _lookup_assignment(connection, data.invitation_token, data.participant_code)
        _require_consent(connection, assignment["assignment_id"])
        rows = _get_stage_rows(connection, assignment["participation_id"])
        existing = rows.get(data.stage_code)

        if existing and existing["status"] == "completed":
            return {
                "stage_code": data.stage_code,
                "status": "completed",
                "already_completed": True,
                "run_id": existing["current_run_id"],
                "attempt_number": existing["attempt_number"],
            }

        stage_index = STAGE_ORDER.index(data.stage_code)
        if stage_index > 0:
            previous = STAGE_ORDER[stage_index - 1]
            previous_state = rows.get(previous)
            if not previous_state or previous_state["status"] != "completed":
                raise HTTPException(409, "مرحلهٔ قبلی هنوز تکمیل نشده است.")

        # For the questionnaire, keep the same run and its saved draft after refresh.
        if (
            data.stage_code == "questionnaire"
            and existing
            and existing["status"] == "in_progress"
            and existing["current_run_id"]
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
            }

        instrument_code, version, instrument_name = STAGE_TO_INSTRUMENT[data.stage_code]
        attempt_number = int(existing["attempt_number"]) + 1 if existing else 1
        timestamp = now()
        old_run_id = existing["current_run_id"] if existing else None

        if old_run_id:
            marker = "%s" if DATABASE_URL else "?"
            connection.execute(
                "UPDATE instrument_runs SET status='superseded',completed_at=COALESCE(completed_at,"
                + marker + ") WHERE run_id=" + marker,
                (timestamp, old_run_id),
            )

        settings = {"module": data.stage_code, "study_code": STUDY_CODE, "schema_version": 1}
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
        metadata = {
            "stage_code": data.stage_code,
            "condition_code": assignment["condition_code"],
            "attempt_number": attempt_number,
        }
        if DATABASE_URL:
            connection.execute(
                "INSERT INTO instrument_runs(run_id,participation_id,instrument_version_id,started_at,status,metadata) "
                "VALUES(%s,%s,%s,%s,'in_progress',%s)",
                (run_id, assignment["participation_id"], instrument_version["instrument_version_id"],
                 timestamp, Jsonb(metadata)),
            )
            connection.execute(
                "INSERT INTO experiment_stage_progress(participation_id,stage_code,status,attempt_number,current_run_id,"
                "draft_payload,started_at,completed_at,updated_at) VALUES(%s,%s,'in_progress',%s,%s,%s,%s,NULL,%s) "
                "ON CONFLICT(participation_id,stage_code) DO UPDATE SET status='in_progress',"
                "attempt_number=EXCLUDED.attempt_number,current_run_id=EXCLUDED.current_run_id,"
                "draft_payload=EXCLUDED.draft_payload,started_at=EXCLUDED.started_at,completed_at=NULL,"
                "updated_at=EXCLUDED.updated_at",
                (assignment["participation_id"], data.stage_code, attempt_number, run_id,
                 Jsonb({}), timestamp, timestamp),
            )
        else:
            connection.execute(
                "INSERT INTO instrument_runs(run_id,participation_id,instrument_version_id,started_at,status,metadata) "
                "VALUES(?,?,?,?,?,?)",
                (run_id, assignment["participation_id"], instrument_version["instrument_version_id"],
                 timestamp, "in_progress", json.dumps(metadata, ensure_ascii=False)),
            )
            connection.execute(
                "INSERT INTO experiment_stage_progress(participation_id,stage_code,status,attempt_number,current_run_id,"
                "draft_payload,started_at,completed_at,updated_at) VALUES(?,?,'in_progress',?,?,?, ?,NULL,?) "
                "ON CONFLICT(participation_id,stage_code) DO UPDATE SET status='in_progress',"
                "attempt_number=excluded.attempt_number,current_run_id=excluded.current_run_id,"
                "draft_payload=excluded.draft_payload,started_at=excluded.started_at,completed_at=NULL,"
                "updated_at=excluded.updated_at",
                (assignment["participation_id"], data.stage_code, attempt_number, run_id,
                 "{}", timestamp, timestamp),
            )
    return {
        "stage_code": data.stage_code,
        "status": "in_progress",
        "resumed": False,
        "already_completed": False,
        "run_id": run_id,
        "attempt_number": attempt_number,
        "draft_payload": {},
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
            "SELECT status,current_run_id FROM experiment_stage_progress "
            "WHERE participation_id=" + marker + " AND stage_code=" + marker,
            (assignment["participation_id"], data.stage_code),
        ).fetchone()
        if not state or state["current_run_id"] != data.run_id:
            raise HTTPException(409, "این اجرای پرسشنامه دیگر فعال نیست.")
        if state["status"] == "completed":
            raise HTTPException(409, "پرسشنامه قبلاً تکمیل شده است.")
        connection.execute(
            "UPDATE experiment_stage_progress SET draft_payload=" + marker + ",updated_at=" + marker
            + " WHERE participation_id=" + marker + " AND stage_code=" + marker,
            (_json_param(data.draft_payload), now(), assignment["participation_id"], data.stage_code),
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
            "SELECT status,current_run_id,draft_payload FROM experiment_stage_progress "
            "WHERE participation_id=" + marker + " AND stage_code=" + marker,
            (assignment["participation_id"], "questionnaire"),
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
            "UPDATE experiment_stage_progress SET draft_payload=" + marker + ",updated_at=" + marker
            + " WHERE participation_id=" + marker + " AND stage_code=" + marker,
            (_json_param(draft), timestamp, assignment["participation_id"], "questionnaire"),
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
        "SELECT status,current_run_id FROM experiment_stage_progress "
        "WHERE participation_id=" + marker + " AND stage_code='ultimatum'",
        (participation_id,),
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
        except Exception as exc:
            raise HTTPException(409, "این دور اولتیماتوم هم‌زمان ایجاد شده است؛ دوباره تلاش کنید.") from exc
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
        if record["status"] == "completed":
            saved_payload = _json_value(record["payload"])
            return saved_payload.get("resolve_result", {})
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
                "SELECT status,payload FROM ultimatum_trials WHERE round_id=" + marker + " AND run_id=" + marker,
                (data.round_id, data.run_id),
            ).fetchone()
            if current and current["status"] == "completed":
                saved_payload = _json_value(current["payload"])
                saved_result = saved_payload.get("resolve_result")
                if saved_result:
                    return saved_result
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


@app.post("/api/experiment/stages/complete")
def complete_experiment_stage(data: StageCompleteIn):
    """Mark a module complete only for its currently active attempt."""
    with conn() as connection:
        assignment = _lookup_assignment(connection, data.invitation_token, data.participant_code)
        _require_consent(connection, assignment["assignment_id"])
        marker = "%s" if DATABASE_URL else "?"
        state = connection.execute(
            "SELECT status,current_run_id,attempt_number,draft_payload FROM experiment_stage_progress "
            "WHERE participation_id=" + marker + " AND stage_code=" + marker,
            (assignment["participation_id"], data.stage_code),
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
        connection.execute(
            "UPDATE instrument_runs SET status='completed',completed_at=" + marker + " WHERE run_id=" + marker,
            (timestamp, data.run_id),
        )
    return {"completed": True, "already_completed": False, "stage_code": data.stage_code}



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


@app.get("/api/admin/progress")
def experiment_progress(x_admin_token: str | None = Header(default=None)):
    """One row per preassigned participant, with authoritative status for all three stages."""
    authorize(x_admin_token)
    marker = "%s" if DATABASE_URL else "?"
    query = (
        "SELECT p.participant_code,a.condition_code,a.assigned_at,"
        "COALESCE(q.status,'not_started') AS questionnaire_status,"
        "COALESCE(q.attempt_number,0) AS questionnaire_attempts,"
        "q.started_at AS questionnaire_started_at,q.completed_at AS questionnaire_completed_at,"
        "COALESCE(d.status,'not_started') AS dd_lifetrade_status,"
        "COALESCE(d.attempt_number,0) AS dd_lifetrade_attempts,"
        "d.started_at AS dd_lifetrade_started_at,d.completed_at AS dd_lifetrade_completed_at,"
        "COALESCE(u.status,'not_started') AS ultimatum_status,"
        "COALESCE(u.attempt_number,0) AS ultimatum_attempts,"
        "u.started_at AS ultimatum_started_at,u.completed_at AS ultimatum_completed_at "
        "FROM experiment_assignments a "
        "JOIN participants p ON p.participant_id=a.participant_id "
        "LEFT JOIN experiment_stage_progress q ON q.participation_id=("
        "SELECT sp.participation_id FROM study_participations sp "
        "WHERE sp.participant_id=a.participant_id AND sp.study_code=a.study_code) "
        "AND q.stage_code='questionnaire' "
        "LEFT JOIN experiment_stage_progress d ON d.participation_id=q.participation_id "
        "AND d.stage_code='dd_lifetrade' "
        "LEFT JOIN experiment_stage_progress u ON u.participation_id=q.participation_id "
        "AND u.stage_code='ultimatum' "
        "WHERE a.study_code=" + marker + " AND a.active=1 ORDER BY a.assigned_at,p.participant_code"
    )
    with conn() as connection:
        rows = connection.execute(query, (STUDY_CODE,)).fetchall()
    return [dict(row) for row in rows]


@app.get("/api/admin/ultimatum/export.csv")
def export_ultimatum_csv(x_admin_token: str | None = Header(default=None)):
    """Export all ultimatum attempts, including practice and superseded attempts for audit."""
    authorize(x_admin_token)
    marker = "%s" if DATABASE_URL else "?"
    query = (
        "SELECT p.participant_code,a.condition_code,ir.run_id,ir.status AS run_status,"
        "ir.started_at AS run_started_at,ir.completed_at AS run_completed_at,"
        "usp.attempt_number,iv.version AS instrument_version,"
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
        "WHERE sp.study_code=" + marker + " ORDER BY ir.started_at,u.phase,u.block_order,u.trial_sequence"
    )
    with conn() as connection:
        rows = connection.execute(query, (STUDY_CODE,)).fetchall()

    fields = [
        "participant_code", "condition_code", "run_id", "run_status", "run_started_at",
        "run_completed_at", "attempt_number", "instrument_version", "round_id", "phase",
        "block_code", "block_order", "trial_in_block", "trial_sequence", "role",
        "opponent_condition", "opponent_actual_type", "round_status", "participant_share",
        "opponent_share", "participant_decision", "opponent_decision", "accepted",
        "points_earned", "potential_points_lost", "reaction_time_ms", "wait_duration_ms",
        "started_at", "decision_screen_shown_at", "responded_at", "received_at",
        "completed_at", "payload_json",
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
            payload = item.pop("payload", {})
            item["payload_json"] = (
                json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
                if not isinstance(payload, str)
                else payload
            )
            writer.writerow(item)
            yield buffer.getvalue()
            buffer.seek(0)
            buffer.truncate(0)

    return StreamingResponse(
        generate_csv(),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": 'attachment; filename="barsan_ultimatum_trials.csv"',
            "Cache-Control": "no-store",
        },
    )


@app.get("/api/admin/questionnaire/export.csv")
def export_questionnaire_csv(x_admin_token: str | None = Header(default=None)):
    """Export completed questionnaire answers in long format, one row per question response."""
    authorize(x_admin_token)
    marker = "%s" if DATABASE_URL else "?"
    query = (
        "SELECT p.participant_code,a.condition_code,qr.question_code,qr.response_value,"
        "qr.updated_at,qr.run_id,iv.version AS instrument_version "
        "FROM questionnaire_responses qr "
        "JOIN instrument_runs ir ON ir.run_id=qr.run_id "
        "JOIN instrument_versions iv ON iv.instrument_version_id=ir.instrument_version_id "
        "JOIN study_participations sp ON sp.participation_id=ir.participation_id "
        "JOIN participants p ON p.participant_id=sp.participant_id "
        "LEFT JOIN experiment_assignments a ON a.participant_id=p.participant_id "
        "AND a.study_code=sp.study_code "
        "WHERE iv.instrument_code=" + marker + " AND sp.study_code=" + marker
        + " AND ir.status='completed' "
        "ORDER BY p.participant_code,qr.question_code"
    )
    with conn() as connection:
        rows = connection.execute(query, ("questionnaire", STUDY_CODE)).fetchall()

    def generate_csv():
        buffer = io.StringIO(newline="")
        buffer.write("\ufeff")
        fields = [
            "participant_code", "condition_code", "question_code", "response_value",
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
            "Content-Disposition": 'attachment; filename="barsan_questionnaire_responses.csv"',
            "Cache-Control": "no-store",
        },
    )


@app.get("/api/admin/sessions")
def sessions(x_admin_token: str | None = Header(default=None)):
    authorize(x_admin_token)
    marker = "%s" if DATABASE_URL else "?"
    with conn() as connection:
        rows = connection.execute(
            "SELECT ir.run_id AS session_id,p.participant_code,ir.started_at AS created_at,"
            "ir.completed_at,ir.status,ir.trial_count,iv.version AS experiment_version "
            "FROM instrument_runs ir "
            "JOIN study_participations sp ON sp.participation_id=ir.participation_id "
            "JOIN participants p ON p.participant_id=sp.participant_id "
            "JOIN instrument_versions iv ON iv.instrument_version_id=ir.instrument_version_id "
            "JOIN instruments i ON i.instrument_code=iv.instrument_code "
            "WHERE i.instrument_code=" + marker + " ORDER BY ir.started_at DESC",
            (INSTRUMENT_CODE,),
        ).fetchall()
    return [dict(row) for row in rows]


@app.get("/api/admin/export.csv")
def export_csv(x_admin_token: str | None = Header(default=None)):
    authorize(x_admin_token)
    with conn() as connection:
        if DATABASE_URL:
            key_rows = connection.execute(
                "SELECT DISTINCT x.json_key FROM lifetrade_trials t "
                "JOIN instrument_runs ir ON ir.run_id=t.run_id "
                "CROSS JOIN LATERAL jsonb_object_keys(t.payload) AS x(json_key) "
                "WHERE ir.status <> 'superseded'"
            ).fetchall()
        else:
            key_rows = connection.execute(
                "SELECT DISTINCT json_each.key AS json_key "
                "FROM lifetrade_trials t JOIN instrument_runs ir ON ir.run_id=t.run_id, "
                "json_each(t.payload) WHERE ir.status <> 'superseded'"
            ).fetchall()
    fields = sorted(
        {row["json_key"] for row in key_rows}
        | {"server_received_at", "server_run_id", "server_session_id", "participant_code", "instrument_code", "instrument_version"}
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
            base_query = (
                "SELECT t.client_event_id,t.run_id,t.received_at,t.payload,p.participant_code,iv.version "
                "FROM lifetrade_trials t "
                "JOIN instrument_runs ir ON ir.run_id=t.run_id "
                "JOIN study_participations sp ON sp.participation_id=ir.participation_id "
                "JOIN participants p ON p.participant_id=sp.participant_id "
                "JOIN instrument_versions iv ON iv.instrument_version_id=ir.instrument_version_id "
                "JOIN instruments i ON i.instrument_code=iv.instrument_code "
                "WHERE i.instrument_code="
            )
            if last_received_at is None:
                query = base_query + ("%s" if DATABASE_URL else "?")
                query += " AND ir.status <> 'superseded' ORDER BY t.received_at,t.client_event_id LIMIT 500"
                params = (INSTRUMENT_CODE,)
            else:
                marker = "%s" if DATABASE_URL else "?"
                query = base_query + marker
                query += (
                    " AND ir.status <> 'superseded' AND (t.received_at,t.client_event_id) > ("
                    + marker
                    + ","
                    + marker
                    + ") ORDER BY t.received_at,t.client_event_id LIMIT 500"
                )
                params = (INSTRUMENT_CODE, last_received_at, last_event_id)

            with conn() as connection:
                rows = connection.execute(query, params).fetchall()
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
                output_rows.append(item)
            writer.writerows(output_rows)
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
