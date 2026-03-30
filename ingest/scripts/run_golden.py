#!/usr/bin/env python3
"""
Golden set CLI runner — Kliniczny Asystent AI.

Runs all cases from tests/golden/cases.json via FastAPI TestClient (no live server needed).
Auth is faked; DB calls are mocked — only the endpoint logic and guardrails are exercised.

Prints one line per case: PASS/FAIL + name.  Never prints raw input or response content.

Usage:
    cd /path/to/repo
    python ingest/scripts/run_golden.py           # compact output
    python ingest/scripts/run_golden.py --verbose  # show failure details
    python ingest/scripts/run_golden.py -v         # same
"""

import json
import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

# ── Bootstrap: env vars must be set before main.py is imported ───────────────
os.environ.setdefault("SUPABASE_URL", "http://localhost:54321")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "golden-runner-key")
os.environ.setdefault("ANTHROPIC_API_KEY", "")

_REPO_ROOT = Path(__file__).parent.parent.parent
_INGEST_DIR = _REPO_ROOT / "ingest"
sys.path.insert(0, str(_INGEST_DIR))

from fastapi.testclient import TestClient  # noqa: E402

from main import Auth, app, get_auth  # noqa: E402

# ── Constants ─────────────────────────────────────────────────────────────────
CONV_ID = "conv-golden-00000000-0000-0000-0000-000000000001"
_CONV_ROW = [{"id": CONV_ID}]
_MSG_ROW = {"id": "msg-golden-001"}
_CASES_FILE = _INGEST_DIR / "tests" / "golden" / "cases.json"

_REQUIRED_PAYLOAD_FIELDS = (
    "questions_to_ask",
    "red_flags",
    "possible_next_steps",
    "patient_facing_summary",
    "sources",
    "flag",
    "disclaimer",
)

# ── Auth override (no real JWT needed) ────────────────────────────────────────
app.dependency_overrides[get_auth] = lambda: Auth(user_id="golden-runner", jwt="fake-jwt")


# ── Case runner ───────────────────────────────────────────────────────────────


def _run_case(client: TestClient, case: dict, verbose: bool) -> bool:
    """Return True on pass, False on fail.  Never prints raw content."""
    expect = case["expect"]
    failures: list[str] = []

    with (
        patch("main._db_select", new=AsyncMock(return_value=_CONV_ROW)),
        patch("main._db_insert", new=AsyncMock(return_value=_MSG_ROW)) as mock_insert,
    ):
        resp = client.post(
            "/chat",
            json={
                "mode": case["mode"],
                "input_text": case["input_text"],
                "conversation_id": CONV_ID,
            },
        )

    # ── Status ────────────────────────────────────────────────────────────────
    if resp.status_code != expect["status"]:
        failures.append(f"status {resp.status_code} != {expect['status']}")
        return _report(case["name"], failures, verbose)

    if expect["status"] == 200:
        data = resp.json()
        payload = data.get("assistant_payload", {})
        flag = payload.get("flag")

        if expect.get("schema_valid"):
            missing = [f for f in _REQUIRED_PAYLOAD_FIELDS if f not in payload]
            if missing:
                failures.append(f"missing fields: {missing}")

        if "flag" in expect and flag != expect["flag"]:
            failures.append(f"flag '{flag}' != '{expect['flag']}'")

        if "flag_in" in expect and flag not in expect["flag_in"]:
            failures.append(f"flag '{flag}' not in {expect['flag_in']}")

        if "flag_not" in expect and flag == expect["flag_not"]:
            failures.append(f"flag must not be '{expect['flag_not']}'")

        if expect.get("has_disclaimer") and not payload.get("disclaimer"):
            failures.append("disclaimer is empty")

        if "min_questions" in expect:
            n = len(payload.get("questions_to_ask", []))
            if n < expect["min_questions"]:
                failures.append(f"questions_to_ask count {n} < {expect['min_questions']}")

        if "injection_flagged" in expect and mock_insert.call_count > 0:
            flags = (
                mock_insert.call_args_list[0].args[1]["content"]["_meta"].get("injection_flags", [])
            )
            if expect["injection_flagged"] and not flags:
                failures.append("expected injection_flags but got []")
            elif not expect["injection_flagged"] and flags:
                failures.append(f"expected no injection_flags but got {flags!r}")

    else:
        if "error_code" in expect:
            actual = resp.json().get("error", {}).get("code")
            if actual != expect["error_code"]:
                failures.append(f"error.code '{actual}' != '{expect['error_code']}'")

    return _report(case["name"], failures, verbose)


def _report(name: str, failures: list[str], verbose: bool) -> bool:
    if failures:
        print(f"  FAIL  {name}")
        if verbose:
            for msg in failures:
                print(f"        → {msg}")
        return False
    print(f"  PASS  {name}")
    return True


# ── Entry point ───────────────────────────────────────────────────────────────


def main() -> None:
    verbose = "--verbose" in sys.argv or "-v" in sys.argv

    if not _CASES_FILE.exists():
        print(f"ERROR: cases file not found: {_CASES_FILE}", file=sys.stderr)
        sys.exit(1)

    cases = json.loads(_CASES_FILE.read_text())["cases"]
    print("\nKliniczny Asystent AI — Golden Set Runner")
    print(f"Running {len(cases)} cases...\n")

    passed = failed = 0
    with TestClient(app) as client:
        for case in cases:
            if _run_case(client, case, verbose):
                passed += 1
            else:
                failed += 1

    print(f"\n{'─' * 42}")
    print(f"  {passed} passed  {failed} failed  {len(cases)} total")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
