"""
Golden set tests (Day 13).

Parametrized over ~20 cases defined in tests/golden/cases.json.
Each case declares expected HTTP status, response flags, schema validity,
and optional injection-flag assertions — no exact-match on content.

Assertion keys supported in each case's "expect" object:
  status          int   Required. Expected HTTP status code.
  schema_valid    bool  All AssistantPayload fields present on 200.
  flag            str   Exact flag value ("safe"|"uncertain"|"refuse").
  flag_in         list  Flag must be one of these values.
  flag_not        str   Flag must NOT be this value.
  has_disclaimer  bool  disclaimer field is non-empty.
  min_questions   int   len(questions_to_ask) >= N.
  error_code      str   error.code in non-200 response.
  injection_flagged bool injection_flags non-empty in stored user _meta.
"""

import json
import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# ---------------------------------------------------------------------------
# Load cases
# ---------------------------------------------------------------------------

_CASES_FILE = Path(__file__).parent / "golden" / "cases.json"
_CASES: list[dict] = json.loads(_CASES_FILE.read_text())["cases"]

CONV_ID = "conv-00000000-0000-0000-0000-000000000001"
_CONV_ROW = [{"id": CONV_ID}]
_MSG_ROW = {"id": "msg-00000000-0000-0000-0000-000000000001"}

_REQUIRED_PAYLOAD_FIELDS = (
    "questions_to_ask",
    "red_flags",
    "possible_next_steps",
    "patient_facing_summary",
    "sources",
    "flag",
    "disclaimer",
)


# ---------------------------------------------------------------------------
# Parametrized test
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("case", _CASES, ids=[c["name"] for c in _CASES])
def test_golden_case(client, case):
    expect = case["expect"]

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

    assert resp.status_code == expect["status"], (
        f"[{case['name']}] status {resp.status_code} != {expect['status']}"
    )

    if expect["status"] == 200:
        data = resp.json()
        assert "assistant_payload" in data, f"[{case['name']}] missing assistant_payload"
        payload = data["assistant_payload"]
        flag = payload.get("flag")

        if expect.get("schema_valid"):
            for field in _REQUIRED_PAYLOAD_FIELDS:
                assert field in payload, f"[{case['name']}] missing schema field: {field}"

        if "flag" in expect:
            assert flag == expect["flag"], f"[{case['name']}] flag '{flag}' != '{expect['flag']}'"

        if "flag_in" in expect:
            assert flag in expect["flag_in"], (
                f"[{case['name']}] flag '{flag}' not in {expect['flag_in']}"
            )

        if "flag_not" in expect:
            assert flag != expect["flag_not"], (
                f"[{case['name']}] flag must not be '{expect['flag_not']}'"
            )

        if expect.get("has_disclaimer"):
            assert payload.get("disclaimer"), f"[{case['name']}] disclaimer is empty"

        if "min_questions" in expect:
            n = len(payload.get("questions_to_ask", []))
            assert n >= expect["min_questions"], (
                f"[{case['name']}] questions_to_ask count {n} < {expect['min_questions']}"
            )

        # injection_flagged: check stored user message _meta (only when DB was written)
        if "injection_flagged" in expect and mock_insert.call_count > 0:
            user_meta = mock_insert.call_args_list[0].args[1]["content"]["_meta"]
            flags = user_meta.get("injection_flags", [])
            if expect["injection_flagged"]:
                assert len(flags) > 0, (
                    f"[{case['name']}] expected injection_flags but got []"
                )
            else:
                assert flags == [], (
                    f"[{case['name']}] expected no injection_flags but got {flags}"
                )

    else:
        data = resp.json()
        if "error_code" in expect:
            actual_code = data.get("error", {}).get("code")
            assert actual_code == expect["error_code"], (
                f"[{case['name']}] error.code '{actual_code}' != '{expect['error_code']}'"
            )
