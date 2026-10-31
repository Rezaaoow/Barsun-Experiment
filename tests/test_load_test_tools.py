import importlib.util
import json
import stat
from pathlib import Path

import pytest


SCRIPT_PATH = Path(__file__).parent / "load" / "create_load_test_users.py"
SPEC = importlib.util.spec_from_file_location("barsan_load_test_users", SCRIPT_PATH)
assert SPEC and SPEC.loader
load_users = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(load_users)


def test_load_user_generator_refuses_production_domain_without_network_call(
    tmp_path, monkeypatch, capsys
):
    output = tmp_path / "participants.json"
    monkeypatch.setenv("BASE_URL", "https://my-experiment.ir/lifetrade")
    monkeypatch.setenv("ADMIN_TOKEN", "must-not-be-printed")
    monkeypatch.setenv("LOAD_TEST_CONFIRM", load_users.CONFIRMATION)
    monkeypatch.setenv("LOAD_TEST_USERS", "600")
    monkeypatch.setenv("PARTICIPANTS_FILE", str(output))

    def forbidden_network(*args, **kwargs):
        pytest.fail("Production domain guard must run before any network request.")

    monkeypatch.setattr(load_users, "urlopen", forbidden_network)

    assert load_users.main() == 2
    assert not output.exists()
    captured = capsys.readouterr()
    assert "production domain" in captured.err
    assert "must-not-be-printed" not in captured.out + captured.err


def test_load_user_generator_requires_explicit_staging_confirmation(
    tmp_path, monkeypatch, capsys
):
    output = tmp_path / "participants.json"
    monkeypatch.setenv("BASE_URL", "https://staging.example.org/lifetrade")
    monkeypatch.setenv("ADMIN_TOKEN", "must-not-be-printed")
    monkeypatch.delenv("LOAD_TEST_CONFIRM", raising=False)
    monkeypatch.setenv("LOAD_TEST_USERS", "600")
    monkeypatch.setenv("PARTICIPANTS_FILE", str(output))

    def forbidden_network(*args, **kwargs):
        pytest.fail("Missing confirmation must be rejected before any network request.")

    monkeypatch.setattr(load_users, "urlopen", forbidden_network)

    assert load_users.main() == 2
    assert not output.exists()
    captured = capsys.readouterr()
    assert "confirmation is missing" in captured.err
    assert "must-not-be-printed" not in captured.out + captured.err


def test_load_user_fixture_is_written_with_owner_only_permissions(tmp_path):
    output = tmp_path / "private" / "participants.json"
    load_users.write_private_json(output, {"complete": False, "participants": []})

    assert output.exists()
    assert stat.S_IMODE(output.stat().st_mode) == 0o600

@pytest.mark.parametrize(
    "health",
    [
        {"ok": True, "database": "sqlite", "database_connected": True},
        {"ok": True, "database": "postgresql", "database_connected": False},
        {"ok": False, "database": "postgresql", "database_connected": True},
    ],
)
def test_load_generator_rejects_unhealthy_or_non_postgresql_staging(
    monkeypatch, health
):
    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return json.dumps(health).encode("utf-8")

    monkeypatch.setattr(
        load_users,
        "urlopen",
        lambda request, timeout: FakeResponse(),
    )
    with pytest.raises(RuntimeError, match="PostgreSQL"):
        load_users.require_postgresql_health("https://staging.example.org/lifetrade/api/health")


def test_load_generator_accepts_healthy_postgresql_staging(monkeypatch):
    expected = {"ok": True, "database": "postgresql", "database_connected": True}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return json.dumps(expected).encode("utf-8")

    monkeypatch.setattr(
        load_users,
        "urlopen",
        lambda request, timeout: FakeResponse(),
    )
    assert load_users.require_postgresql_health(
        "https://staging.example.org/lifetrade/api/health"
    ) == expected

def test_host_diagnostics_uses_mapping_rows_without_exposing_connection_string(monkeypatch):
    import sys
    import types

    diagnostics_path = Path(__file__).parent / "load" / "host_diagnostics.py"
    diagnostics_spec = importlib.util.spec_from_file_location(
        "barsan_host_diagnostics_test", diagnostics_path
    )
    assert diagnostics_spec and diagnostics_spec.loader
    diagnostics = importlib.util.module_from_spec(diagnostics_spec)
    diagnostics_spec.loader.exec_module(diagnostics)

    expected_factory = object()

    class Cursor:
        def __init__(self, row):
            self.row = row

        def fetchone(self):
            return self.row

    class FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def execute(self, query):
            if "current_setting('server_version')" in query:
                return Cursor({
                    "server_version": "16.0",
                    "max_connections": 100,
                    "reserved_connections": 3,
                    "shared_buffers": "128MB",
                    "work_mem": "4MB",
                    "effective_cache_size": "4GB",
                })
            return Cursor({
                "total": 2,
                "active": 1,
                "idle": 1,
                "idle_in_transaction": 0,
            })

    def fake_connect(database_url, *, connect_timeout, row_factory):
        assert database_url == "postgresql://secret.invalid/db"
        assert connect_timeout == 5
        assert row_factory is expected_factory
        return FakeConnection()

    psycopg_module = types.ModuleType("psycopg")
    psycopg_module.connect = fake_connect
    rows_module = types.ModuleType("psycopg.rows")
    rows_module.dict_row = expected_factory
    monkeypatch.setitem(sys.modules, "psycopg", psycopg_module)
    monkeypatch.setitem(sys.modules, "psycopg.rows", rows_module)

    snapshot = diagnostics._postgres_snapshot("postgresql://secret.invalid/db")
    assert snapshot["connected"] is True
    assert snapshot["server_version"] == "16.0"
    assert snapshot["connections"] == {
        "total": 2,
        "active": 1,
        "idle": 1,
        "idle_in_transaction": 0,
    }
    assert "secret.invalid" not in json.dumps(snapshot)

