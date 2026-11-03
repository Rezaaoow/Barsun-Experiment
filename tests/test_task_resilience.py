import os
import tempfile
import uuid

os.environ["DATABASE_URL"] = ""
os.environ.setdefault("SQLITE_PATH", os.path.join(tempfile.gettempdir(), "resilience-" + uuid.uuid4().hex + ".sqlite3"))

import pytest
from fastapi.testclient import TestClient
import barsan_experiment as main
from resilience_checks import check_dd_recovery, check_ultimatum_recovery


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DB_PATH", str(tmp_path / "tasks.sqlite3"))
    monkeypatch.setattr(main, "ADMIN_TOKEN", "synthetic-admin-token")
    monkeypatch.setattr(main, "INVITATION_TOKEN_KEY_FILE", str(tmp_path / "test.key"))
    main.init_db()
    with TestClient(main.app) as client:
        yield client


@pytest.mark.parametrize("condition", ["A", "B"])
def test_full_ultimatum_prefetch_replay_atomic_timing_and_ordered_export(client, condition):
    check_ultimatum_recovery(client, condition)


def test_dd_strict_completion_and_lossless_ordered_export(client):
    check_dd_recovery(client)


@pytest.mark.parametrize("path", ["/api/admin/dd/records.csv", "/api/admin/ultimatum/records.csv"])
def test_ordered_export_requires_admin_and_valid_scope(client, path):
    assert client.get(path).status_code == 401
    response = client.get(path + "?scope=invalid", headers={"X-Admin-Token": main.ADMIN_TOKEN})
    assert response.status_code == 422


def test_font_is_local_versioned_and_root_path_safe(client):
    response = client.get("/assets/vazirmatn-v33.003.woff2")
    assert response.status_code == 200 and response.content.startswith(b"wOF2")
    assert "immutable" in response.headers["cache-control"]
    with TestClient(main.app, root_path="/lifetrade") as nested:
        page = nested.get("/").text
    assert "/lifetrade/assets/vazirmatn-v33.003.woff2" in page
    assert "cdn.jsdelivr.net" not in page
