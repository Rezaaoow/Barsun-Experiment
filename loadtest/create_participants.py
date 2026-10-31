#!/usr/bin/env python3
"""Create isolated synthetic invitations for a staging load test.

Required environment:
  BASE_URL=https://staging.example.com/lifetrade
  ADMIN_TOKEN=<staging admin token>

The script never prints or logs invitation tokens. It writes them only to a
mode-0600 JSON file that is excluded from Git.
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

STEPS = [
    {"step_id": "load-questionnaire", "module_code": "questionnaire"},
    {"step_id": "load-lifetrade", "module_code": "dd_lifetrade"},
    {"step_id": "load-ultimatum", "module_code": "ultimatum"},
]
PRODUCTION_HOSTS = {"my-experiment.ir", "www.my-experiment.ir"}


def request_json(url: str, token: str, payload: dict[str, Any]) -> dict[str, Any]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "X-Admin-Token": token,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=90) as response:
            data = response.read()
    except urllib.error.HTTPError as exc:
        # Do not echo the response body: it may contain invitation URLs.
        raise RuntimeError(f"Assignment API returned HTTP {exc.code}") from None
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Could not reach the staging API: {exc.reason}") from None
    try:
        value = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise RuntimeError("Assignment API returned invalid JSON") from None
    if not isinstance(value, dict):
        raise RuntimeError("Assignment API returned an unexpected response")
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--count", type=int, default=600)
    parser.add_argument("--output", default="loadtest/participants.json")
    parser.add_argument("--base-url", default=os.getenv("BASE_URL"))
    args = parser.parse_args()

    if not args.base_url:
        parser.error("Set BASE_URL to the staging application URL.")
    if not 1 <= args.count <= 6000:
        parser.error("--count must be between 1 and 6000.")

    parsed = urllib.parse.urlparse(args.base_url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not host:
        parser.error("BASE_URL must be an HTTPS staging URL.")
    if host in PRODUCTION_HOSTS:
        parser.error("Refusing to create synthetic load-test assignments on production.")
    if not parsed.path.rstrip("/").endswith("/lifetrade") and parsed.path.rstrip("/") != "/lifetrade":
        parser.error("BASE_URL must include the staging /lifetrade application path.")

    admin_token = os.getenv("ADMIN_TOKEN", "")
    if not admin_token:
        parser.error("Set ADMIN_TOKEN in the shell environment; do not put it in a command or file.")

    output = Path(args.output).expanduser()
    if output.exists():
        parser.error(f"Output already exists; choose a new filename: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)

    prefix = "LOAD-" + secrets.token_hex(3).upper()
    participants: list[dict[str, str]] = []
    api_url = args.base_url.rstrip("/") + "/api/admin/assignments"

    for start in range(1, args.count + 1, 500):
        end = min(args.count, start + 499)
        assignments = [
            {
                "participant_code": f"{prefix}-{index:04d}",
                "condition_code": "A" if index % 2 else "B",
            }
            for index in range(start, end + 1)
        ]
        result = request_json(
            api_url,
            admin_token,
            {"assignments": assignments, "steps": STEPS},
        )
        returned = result.get("assignments")
        if result.get("created_count") != len(assignments) or not isinstance(returned, list):
            raise RuntimeError("Assignment API did not create the requested batch.")
        for item in returned:
            url = urllib.parse.urlparse(str(item.get("invitation_url") or ""))
            token_values = urllib.parse.parse_qs(url.query).get("invite", [])
            if not token_values:
                raise RuntimeError("An invitation token was missing from the staging response.")
            participants.append({
                "participant_code": str(item["participant_code"]),
                "invitation_token": token_values[0],
            })

    if len(participants) != args.count:
        raise RuntimeError("The number of created staging invitations does not match the request.")

    temporary = output.with_suffix(output.suffix + ".tmp")
    temporary.write_text(json.dumps(participants, ensure_ascii=False), encoding="utf-8")
    os.chmod(temporary, 0o600)
    temporary.replace(output)
    os.chmod(output, 0o600)

    print(f"Created {len(participants)} synthetic staging invitations.")
    print(f"Private participant file: {output}")
    print("Invitation tokens were not printed. Keep this file out of Git and delete it after the load test.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(1)
