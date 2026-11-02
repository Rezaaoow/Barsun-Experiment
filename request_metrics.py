"""Opt-in, identity-free ASGI and database timings for private operational logs."""
import json
import logging
import os
import re
import threading
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar


_current = ContextVar("barsan_request_metrics", default=None)
_logger = logging.getLogger("barsan.request_metrics")
_trace_pattern = re.compile(rb"[a-f0-9]{32}")


class _Connection:
    def __init__(self, connection, metrics):
        self._connection = connection
        self._metrics = metrics

    def execute(self, *args, **kwargs):
        started = time.perf_counter()
        try:
            return self._connection.execute(*args, **kwargs)
        finally:
            self._metrics["sql_ms"] += (time.perf_counter() - started) * 1000
            self._metrics["sql_count"] += 1

    def __getattr__(self, name):
        return getattr(self._connection, name)


@contextmanager
def observed_connection(context):
    metrics = _current.get()
    if metrics is None:
        with context as connection:
            yield connection
        return
    started = time.perf_counter()
    if metrics["first_db_ms"] is None:
        metrics["first_db_ms"] = (started - metrics["started"]) * 1000
    acquired = False
    try:
        with context as connection:
            metrics["pool_wait_ms"] += (time.perf_counter() - started) * 1000
            acquired = True
            yield _Connection(connection, metrics)
    finally:
        if not acquired:
            metrics["pool_wait_ms"] += (time.perf_counter() - started) * 1000
        # Includes checkout, SQL, application work inside the scope and commit.
        metrics["db_scope_ms"] += (time.perf_counter() - started) * 1000


class RequestMetricsMiddleware:
    def __init__(self, app):
        self.app = app
        self._active = 0
        self._lock = threading.Lock()
        if not _logger.handlers:
            _logger.addHandler(logging.StreamHandler())
        _logger.setLevel(logging.INFO)
        _logger.propagate = False

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        trace = next((value for key, value in scope.get("headers", [])
                      if key.lower() == b"x-barsan-trace"), b"")
        request_id = trace.decode("ascii") if _trace_pattern.fullmatch(trace) else uuid.uuid4().hex
        with self._lock:
            self._active += 1
            active_at_start = self._active
        metrics = {"started": time.perf_counter(), "first_db_ms": None,
                   "pool_wait_ms": 0.0, "sql_ms": 0.0, "sql_count": 0,
                   "db_scope_ms": 0.0}
        token = _current.set(metrics)
        status = 500
        error_type = None
        _logger.info(json.dumps({"event": "request_start", "rid": request_id,
                                "at": time.time(), "pid": os.getpid(),
                                "inflight": active_at_start}, separators=(",", ":")))

        async def measured_send(message):
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                message = dict(message)
                message["headers"] = list(message.get("headers", [])) + [
                    (b"x-barsan-trace", request_id.encode("ascii")),
                    (b"server-timing", (
                        f'app;dur={(time.perf_counter() - metrics["started"]) * 1000:.3f},'
                        f'pool;dur={metrics["pool_wait_ms"]:.3f},sql;dur={metrics["sql_ms"]:.3f}'
                    ).encode("ascii")),
                ]
            await send(message)

        try:
            await self.app(scope, receive, measured_send)
        except BaseException as exc:
            error_type = type(exc).__name__
            raise
        finally:
            with self._lock:
                self._active -= 1
                active_at_end = self._active
            route = scope.get("route")
            record = {"event": "request_end", "rid": request_id, "at": time.time(),
                      "pid": os.getpid(), "route": getattr(route, "path", "unmatched"),
                      "status": status, "error_type": error_type,
                      "inflight_start": active_at_start, "inflight_end": active_at_end,
                      "app_ms": round((time.perf_counter() - metrics["started"]) * 1000, 3)}
            for key in ("first_db_ms", "pool_wait_ms", "sql_ms", "db_scope_ms", "sql_count"):
                record[key] = round(metrics[key], 3) if metrics[key] is not None else None
            _current.reset(token)
            _logger.info(json.dumps(record, separators=(",", ":")))
