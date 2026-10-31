#!/usr/bin/env python3
"""Read-only cPanel/Passenger and PostgreSQL capacity snapshot.

Run inside the app's Python 3.12 virtualenv. DATABASE_URL is read from the
environment and is never printed. The script does not change app or DB state.
"""
from __future__ import annotations

import json
import os
import resource
from pathlib import Path
from typing import Any


def _read_text(path: str) -> str | None:
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        return None


def _memory_limit_bytes() -> int | None:
    for path in (
        "/sys/fs/cgroup/memory.max",
        "/sys/fs/cgroup/memory/memory.limit_in_bytes",
    ):
        value = _read_text(path)
        if not value or value == "max":
            continue
        try:
            limit = int(value)
        except ValueError:
            continue
        # Some cgroup v1 systems use a very large sentinel for "unlimited".
        if 0 < limit < 1 << 60:
            return limit
    return None


def _cpu_quota_cores() -> float | None:
    value = _read_text("/sys/fs/cgroup/cpu.max")
    if value:
        parts = value.split()
        if len(parts) == 2 and parts[0] != "max":
            try:
                quota, period = int(parts[0]), int(parts[1])
                return quota / period if quota > 0 and period > 0 else None
            except ValueError:
                pass

    quota = _read_text("/sys/fs/cgroup/cpu/cpu.cfs_quota_us")
    period = _read_text("/sys/fs/cgroup/cpu/cpu.cfs_period_us")
    try:
        quota_value, period_value = int(quota or ""), int(period or "")
        if quota_value > 0 and period_value > 0:
            return quota_value / period_value
    except ValueError:
        pass
    return None


def _processes_for_uid() -> int | None:
    proc = Path("/proc")
    if not proc.is_dir():
        return None
    uid = os.geteuid()
    total = 0
    try:
        entries = list(proc.iterdir())
    except OSError:
        return None
    for entry in entries:
        if not entry.name.isdigit():
            continue
        status = _read_text(str(entry / "status"))
        if not status:
            continue
        for line in status.splitlines():
            if line.startswith("Uid:"):
                fields = line.split()
                if len(fields) >= 3:
                    try:
                        if int(fields[2]) == uid:
                            total += 1
                    except ValueError:
                        pass
                break
    return total


def _limit_to_json(value: tuple[int, int]) -> dict[str, int | None]:
    soft, hard = value
    return {
        "soft": None if soft == resource.RLIM_INFINITY else int(soft),
        "hard": None if hard == resource.RLIM_INFINITY else int(hard),
    }


def _postgres_snapshot(database_url: str) -> dict[str, Any]:
    try:
        import psycopg

        with psycopg.connect(database_url, connect_timeout=5) as connection:
            settings = connection.execute(
                "SELECT current_setting('server_version') AS server_version,"
                "current_setting('max_connections')::integer AS max_connections,"
                "current_setting('superuser_reserved_connections')::integer AS reserved_connections,"
                "current_setting('shared_buffers') AS shared_buffers,"
                "current_setting('work_mem') AS work_mem,"
                "current_setting('effective_cache_size') AS effective_cache_size"
            ).fetchone()
            activity = connection.execute(
                "SELECT COUNT(*)::integer AS total,"
                "COUNT(*) FILTER (WHERE state='active')::integer AS active,"
                "COUNT(*) FILTER (WHERE state='idle')::integer AS idle,"
                "COUNT(*) FILTER (WHERE state='idle in transaction')::integer AS idle_in_transaction "
                "FROM pg_stat_activity WHERE datname=current_database()"
            ).fetchone()
        return {
            "connected": True,
            "server_version": settings["server_version"],
            "max_connections": settings["max_connections"],
            "reserved_connections": settings["reserved_connections"],
            "shared_buffers": settings["shared_buffers"],
            "work_mem": settings["work_mem"],
            "effective_cache_size": settings["effective_cache_size"],
            "connections": dict(activity),
        }
    except Exception as error:
        # Exception messages may embed connection details; emit only the class name.
        return {
            "connected": False,
            "error_type": type(error).__name__,
        }


def main() -> int:
    cpu_affinity = None
    try:
        cpu_affinity = len(os.sched_getaffinity(0))
    except (AttributeError, OSError):
        cpu_affinity = os.cpu_count()

    cpu_quota = _cpu_quota_cores()
    cpu_effective = min(cpu_affinity, cpu_quota) if (
        cpu_affinity is not None and cpu_quota is not None
    ) else (cpu_quota if cpu_quota is not None else cpu_affinity)

    memory_bytes = _memory_limit_bytes()
    nofile = _limit_to_json(resource.getrlimit(resource.RLIMIT_NOFILE))
    nproc = (
        _limit_to_json(resource.getrlimit(resource.RLIMIT_NPROC))
        if hasattr(resource, "RLIMIT_NPROC")
        else None
    )

    pool = {
        "min_size": int(os.getenv("DB_POOL_MIN_SIZE", "1")),
        "max_size_per_process": int(os.getenv("DB_POOL_MAX_SIZE", "4")),
        "timeout_seconds": float(os.getenv("DB_POOL_TIMEOUT", "5")),
        "max_waiting_per_process": int(os.getenv("DB_POOL_MAX_WAITING", "128")),
        "connect_timeout_seconds": int(os.getenv("DB_CONNECT_TIMEOUT", "5")),
    }

    database_url = os.getenv("DATABASE_URL", "").strip()
    database = (
        _postgres_snapshot(database_url)
        if database_url
        else {"connected": False, "error_type": "DATABASE_URL_MISSING"}
    )
    result = {
        "host": {
            "cpu_affinity_count": cpu_affinity,
            "cpu_quota_cores": round(cpu_quota, 3) if cpu_quota is not None else None,
            "effective_cpu_limit_estimate": round(cpu_effective, 3) if cpu_effective is not None else None,
            "cgroup_memory_limit_bytes": memory_bytes,
            "cgroup_memory_limit_gib": round(memory_bytes / (1024 ** 3), 3) if memory_bytes else None,
            "processes_for_current_uid": _processes_for_uid(),
            "rlimit_nofile": nofile,
            "rlimit_nproc": nproc,
        },
        "application": {
            "python_version": __import__("platform").python_version(),
            "db_pool": pool,
            "estimated_total_pool_connections": None,
            "note": "Total pool connections equal max_size_per_process multiplied by Passenger app process count, plus other clients. The process count below includes all processes for this cPanel UID, not Passenger-only processes.",
        },
        "postgresql": database,
    }
    process_count = result["host"]["processes_for_current_uid"]
    if process_count is not None:
        result["application"]["estimated_total_pool_connections"] = (
            pool["max_size_per_process"] * process_count
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if database.get("connected") else 1


if __name__ == "__main__":
    raise SystemExit(main())
