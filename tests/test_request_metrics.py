import io
import json
import logging
import sqlite3
from contextlib import contextmanager

from fastapi import FastAPI
from fastapi.testclient import TestClient

import request_metrics as metrics


@contextmanager
def database():
    connection = sqlite3.connect(":memory:")
    try:
        yield connection
        connection.commit()
    finally:
        connection.close()


def test_private_metrics_cross_thread_context_and_do_not_log_inputs(monkeypatch):
    stream = io.StringIO()
    monkeypatch.setattr(metrics._logger, "handlers", [logging.StreamHandler(stream)])
    app = FastAPI()
    app.add_middleware(metrics.RequestMetricsMiddleware)

    @app.post("/test/{item}")
    def endpoint(item: str, payload: dict):
        with metrics.observed_connection(database()) as connection:
            return {"value": connection.execute("SELECT ?", (payload["secret"],)).fetchone()[0]}

    trace = "a" * 32
    with TestClient(app) as client:
        response = client.post("/test/PRIVATE-PATH?invite=PRIVATE-QUERY",
                               json={"secret": "PRIVATE-BODY"},
                               headers={"x-barsan-trace": trace, "x-admin-token": "PRIVATE-HEADER"})
    assert response.status_code == 200
    assert response.json() == {"value": "PRIVATE-BODY"}
    assert response.headers["x-barsan-trace"] == trace
    assert "sql;dur=" in response.headers["server-timing"]
    records = [json.loads(line) for line in stream.getvalue().splitlines()]
    assert [r["event"] for r in records] == ["request_start", "request_end"]
    end = records[-1]
    assert end["route"] == "/test/{item}"
    assert end["sql_count"] == 1
    assert end["first_db_ms"] is not None
    assert 0 <= end["sql_ms"] <= end["db_scope_ms"] <= end["app_ms"]
    assert end["inflight_end"] == 0
    assert "PRIVATE-" not in stream.getvalue()
    assert metrics._current.get() is None


def test_metrics_reset_after_error_and_reject_untrusted_trace(monkeypatch):
    stream = io.StringIO()
    monkeypatch.setattr(metrics._logger, "handlers", [logging.StreamHandler(stream)])
    app = FastAPI()
    app.add_middleware(metrics.RequestMetricsMiddleware)

    @app.get("/fail")
    def endpoint():
        raise RuntimeError("PRIVATE-EXCEPTION")

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/fail", headers={"x-barsan-trace": "PRIVATE-TRACE"})
    assert response.status_code == 500
    records = [json.loads(line) for line in stream.getvalue().splitlines()]
    assert len(records[-1]["rid"]) == 32
    assert records[-1]["error_type"] == "RuntimeError"
    assert records[-1]["inflight_end"] == 0
    assert "PRIVATE-" not in stream.getvalue()
    assert metrics._current.get() is None


def test_database_connection_is_unchanged_without_request_metrics():
    @contextmanager
    def raw():
        yield sentinel
    sentinel = object()
    with metrics.observed_connection(raw()) as connection:
        assert connection is sentinel


def test_configured_sync_threads_bound_concurrency_without_dropping_work(monkeypatch):
    import anyio
    import threading
    import time
    import barsan_experiment as main

    monkeypatch.setenv("BARSAN_SYNC_THREADS", "3")
    state = {"active": 0, "peak": 0, "finished": 0}
    lock = threading.Lock()

    def work():
        with lock:
            state["active"] += 1
            state["peak"] = max(state["peak"], state["active"])
        time.sleep(0.02)
        with lock:
            state["active"] -= 1
            state["finished"] += 1

    async def run():
        await main.configure_sync_threads()
        assert anyio.to_thread.current_default_thread_limiter().total_tokens == 3
        async with anyio.create_task_group() as group:
            for _ in range(12):
                group.start_soon(anyio.to_thread.run_sync, work)

    anyio.run(run)
    assert state == {"active": 0, "peak": 3, "finished": 12}
