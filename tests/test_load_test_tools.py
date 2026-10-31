import importlib.util
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
