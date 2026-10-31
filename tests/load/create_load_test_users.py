#!/usr/bin/env python3
"""Create isolated, staging-only participant invitations for the BarSan k6 test.

Never run this script against the live my-experiment.ir host or production database.
The generated JSON contains bearer-like invitation tokens and is written with mode 0600.
"""
from __future__ import annotations

import json
import os
import secrets
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request, urlopen

CONFIRMATION = "I CONFIRM THIS IS A DEDICATED STAGING ENVIRONMENT"
PRODUCTION_HOSTS = {"my-experiment.ir"}
BATCH_SIZE = 500
WORKFLOW_STEPS = [
    {"step_id": "load-questionnaire", "module_code": "questionnaire"},
    {"step_id": "load-dd", "module_code": "dd_lifetrade"},
    {"step_id": "load-ultimatum", "module_code": "ultimatum_frame"},
]


def write_private_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".tmp-{os.getpid()}")
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
        0o600,
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(payload, output, ensure_ascii=False, separators=(",", ":"))
            output.write("\n")
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def post_assignments(endpoint: str, admin_token: str, assignments: list[dict]) -> dict:
    payload = json.dumps(
        {"assignments": assignments, "steps": WORKFLOW_STEPS},
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    request = Request(
        endpoint,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "X-Admin-Token": admin_token,
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        raise RuntimeError(f"assignment endpoint returned HTTP {error.code}") from None
    except (URLError, TimeoutError, json.JSONDecodeError):
        raise RuntimeError("assignment request failed or returned invalid JSON") from None


def main() -> int:
    base_url = os.getenv("BASE_URL", "").strip().rstrip("/")
    admin_token = os.getenv("ADMIN_TOKEN", "")
    output_path = Path(os.getenv("PARTICIPANTS_FILE", "tests/load/participants.json")).expanduser()
    try:
        count = int(os.getenv("LOAD_TEST_USERS", "600"))
    except ValueError:
        print("LOAD_TEST_USERS must be an integer.", file=sys.stderr)
        return 2

    if os.getenv("LOAD_TEST_CONFIRM") != CONFIRMATION:
        print("Explicit staging confirmation is missing; no requests were sent.", file=sys.stderr)
        return 2
    if not base_url.startswith("https://"):
        print("BASE_URL must be an HTTPS staging application URL.", file=sys.stderr)
        return 2
    host = (urlsplit(base_url).hostname or "").lower()
    if not host or host in PRODUCTION_HOSTS or any(
        host.endswith("." + production) for production in PRODUCTION_HOSTS
    ):
        print("The configured host is the production domain; refusing to create load-test links.", file=sys.stderr)
        return 2
    if not admin_token:
        print("ADMIN_TOKEN is missing from the environment.", file=sys.stderr)
        return 2
    if count < 1 or count > 600:
        print("LOAD_TEST_USERS must be between 1 and 600.", file=sys.stderr)
        return 2
    if output_path.exists():
        print(
            f"Refusing to overwrite existing participant file: {output_path}. "
            "Move it securely before generating a new cohort.",
            file=sys.stderr,
        )
        return 2

    endpoint = base_url + "/api/admin/assignments"
    run_code = datetime.now(timezone.utc).strftime("%y%m%d%H%M%S") + "-" + secrets.token_hex(3)
    participants: list[dict[str, str]] = []
    saved = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "base_host": host,
        "expected_count": count,
        "created_count": 0,
        "complete": False,
        "participants": participants,
    }
    write_private_json(output_path, saved)

    try:
        for offset in range(0, count, BATCH_SIZE):
            batch = [
                {"participant_code": f"LOAD-{run_code}-{index + 1:04d}"}
                for index in range(offset, min(offset + BATCH_SIZE, count))
            ]
            result = post_assignments(endpoint, admin_token, batch)
            links = result.get("assignments") if isinstance(result, dict) else None
            if not isinstance(links, list) or len(links) != len(batch):
                raise RuntimeError("assignment endpoint returned an incomplete batch")

            for item in links:
                invitation_url = str(item.get("invitation_url") or "")
                token = parse_qs(urlsplit(invitation_url).query).get("invite", [""])[0]
                participant_code = str(item.get("participant_code") or "")
                if not token or not participant_code:
                    raise RuntimeError("assignment response did not contain a usable invitation")
                participants.append({
                    "participant_code": participant_code,
                    "invitation_token": token,
                })

            saved["created_count"] = len(participants)
            write_private_json(output_path, saved)
            print(f"Created {len(participants)} of {count} staging invitations.")

        saved["complete"] = True
        write_private_json(output_path, saved)
        print(f"Ready: {count} invitations saved privately to {output_path}.")
        print("The file contains invitation tokens; do not commit, email, or publish it.")
        return 0
    except Exception as error:
        saved["complete"] = False
        write_private_json(output_path, saved)
        print(
            f"Stopped after {len(participants)} of {count} invitations. "
            f"Partial file retained at {output_path}. Reason: {error}",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
