"""
Tests for response validation and repair (Day 10).

Integration cases (via /chat endpoint with mocked DB + patched _build_raw_payload):
  a) invalid → repair → valid → 200, repair_applied=True in metadata
  b) invalid → not repairable → 500, safe error message, assistant message NOT stored
  c) normal valid response → 200, repair_applied=False in metadata
  d) _meta in assistant message has validation flags, not raw payload content

Unit cases (validator.py functions in isolation — no FastAPI needed):
  - null list fields coerced to []
  - string list field coerced to [string]
  - invalid string flag → "uncertain"
  - non-string flag (int) → NOT repaired (remains invalid)
  - missing/empty disclaimer → fallback
  - source item missing fields → filled with ""
  - non-dict source item → dropped
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from unittest.mock import AsyncMock, patch

import pytest
from pydantic import ValidationError

from validator import repair_payload, validate_and_repair

# ---------------------------------------------------------------------------
# Shared test data
# ---------------------------------------------------------------------------

CONV_ID = "conv-00000000-0000-0000-0000-000000000001"
_CONV_ROW = [{"id": CONV_ID}]
_MSG_ROW = {"id": "msg-00000000-0000-0000-0000-000000000001"}

VALID_BODY = {
    "mode": "triage",
    "input_text": "Patient presents with chest pain, duration two hours.",
    "conversation_id": CONV_ID,
}

# (a) Repairable: null list, string-as-list, source dict missing "section".
REPAIRABLE_RAW = {
    "questions_to_ask": None,  # null → []
    "red_flags": "Ból w klatce piersiowej",  # string → ["Ból w klatce piersiowej"]
    "possible_next_steps": [],
    "patient_facing_summary": "Podsumowanie wizyty.",
    "sources": [{"id": "s1", "title": "PTK 2024"}],  # missing "section" → ""
    "flag": "safe",
    "disclaimer": "Disclaimer.",
}

# (b) Unrepairable: flag is an integer — repair intentionally skips non-string flags.
UNREPAIRABLE_RAW = {
    "questions_to_ask": [],
    "red_flags": [],
    "possible_next_steps": [],
    "patient_facing_summary": "Summary.",
    "sources": [],
    "flag": 42,  # integer — not repaired; Pydantic rejects it
    "disclaimer": "Disclaimer.",
}


# ---------------------------------------------------------------------------
# Unit tests — repair_payload in isolation
# ---------------------------------------------------------------------------


def test_repair_null_list_fields():
    data = {**REPAIRABLE_RAW, "questions_to_ask": None, "red_flags": None}
    out = repair_payload(data)
    assert out["questions_to_ask"] == []
    assert out["red_flags"] == []


def test_repair_string_coerced_to_list():
    out = repair_payload({**REPAIRABLE_RAW, "red_flags": "High fever"})
    assert out["red_flags"] == ["High fever"]


def test_repair_invalid_string_flag_becomes_uncertain():
    out = repair_payload({**REPAIRABLE_RAW, "flag": "UNKNOWN_VALUE"})
    assert out["flag"] == "uncertain"


def test_repair_int_flag_not_coerced():
    """Integer flag is intentionally left alone — triggers 500 in the endpoint."""
    out = repair_payload(UNREPAIRABLE_RAW)
    assert out["flag"] == 42


def test_repair_empty_disclaimer_gets_fallback():
    out = repair_payload({**REPAIRABLE_RAW, "disclaimer": ""})
    assert out["disclaimer"]  # non-empty
    assert "konsultuj" in out["disclaimer"].lower()


def test_repair_source_missing_fields_filled():
    out = repair_payload({**REPAIRABLE_RAW, "sources": [{"id": "x"}]})
    # text_snippet is None when absent — matches the Source model default.
    assert out["sources"] == [{"id": "x", "title": "", "section": "", "text_snippet": None}]


def test_repair_non_dict_source_dropped():
    out = repair_payload(
        {
            **REPAIRABLE_RAW,
            "sources": ["not-a-dict", 42, {"id": "ok", "title": "T", "section": "S"}],
        }
    )
    assert len(out["sources"]) == 1
    assert out["sources"][0]["id"] == "ok"


def test_validate_and_repair_valid_no_repair():
    from main import AssistantPayload

    valid = {
        "questions_to_ask": [],
        "red_flags": [],
        "possible_next_steps": [],
        "patient_facing_summary": "Summary.",
        "sources": [],
        "flag": "safe",
        "disclaimer": "Disclaimer.",
    }
    instance, repair_applied = validate_and_repair(valid, AssistantPayload)
    assert repair_applied is False
    assert instance.flag == "safe"


def test_validate_and_repair_applies_repair():
    from main import AssistantPayload

    instance, repair_applied = validate_and_repair(REPAIRABLE_RAW, AssistantPayload)
    assert repair_applied is True
    assert instance.questions_to_ask == []
    assert instance.red_flags == ["Ból w klatce piersiowej"]
    assert instance.sources[0].section == ""


def test_validate_and_repair_unrepairable_raises():
    from main import AssistantPayload

    with pytest.raises(ValidationError):
        validate_and_repair(UNREPAIRABLE_RAW, AssistantPayload)


# ---------------------------------------------------------------------------
# Integration tests — /chat endpoint with mocked DB
# ---------------------------------------------------------------------------


def test_chat_valid_response_no_repair(client):
    """(c) Normal mock path → 200, repair_applied=False in assistant _meta."""
    with (
        patch("main._db_select", new=AsyncMock(return_value=_CONV_ROW)),
        patch("main._db_insert", new=AsyncMock(return_value=_MSG_ROW)) as mock_insert,
    ):
        resp = client.post("/chat", json=VALID_BODY)

    assert resp.status_code == 200
    assistant_row = mock_insert.call_args_list[1].args[1]
    meta = assistant_row["content"]["_meta"]
    assert meta["repair_applied"] is False
    assert meta["validation_status"] == "valid"


def test_chat_invalid_repaired_to_valid(client):
    """(a) Repairable invalid payload → 200, repair_applied=True, schema intact."""
    with (
        patch("main._db_select", new=AsyncMock(return_value=_CONV_ROW)),
        patch("main._db_insert", new=AsyncMock(return_value=_MSG_ROW)) as mock_insert,
        patch("main._build_raw_payload", return_value=REPAIRABLE_RAW),
    ):
        resp = client.post("/chat", json=VALID_BODY)

    assert resp.status_code == 200
    payload = resp.json()["assistant_payload"]
    # After repair the payload is valid; no-source fallback may override it (no embedder in
    # tests), so we assert only that schema is intact and repair was recorded in meta.
    assert payload["flag"] in ("safe", "uncertain", "refuse")
    assert isinstance(payload["questions_to_ask"], list)

    meta = mock_insert.call_args_list[1].args[1]["content"]["_meta"]
    assert meta["repair_applied"] is True
    assert meta["validation_status"] == "valid"


def test_chat_unrepairable_returns_500(client):
    """(b) Unrepairable payload → 500 with safe message; assistant row NOT stored."""
    with (
        patch("main._db_select", new=AsyncMock(return_value=_CONV_ROW)),
        patch("main._db_insert", new=AsyncMock(return_value=_MSG_ROW)) as mock_insert,
        patch("main._build_raw_payload", return_value=UNREPAIRABLE_RAW),
    ):
        resp = client.post("/chat", json=VALID_BODY)

    assert resp.status_code == 500
    error = resp.json()["error"]
    assert error["code"] == "VALIDATION_FAILED"
    # Generic safe message — no raw values
    assert "schema" in error["message"].lower() or "retry" in error["message"].lower()
    assert "42" not in error["message"]

    # Only user message was inserted; bad assistant payload must never be stored
    assert len(mock_insert.call_args_list) == 1, (
        "Assistant row must not be stored on validation failure"
    )


def test_chat_assistant_meta_has_flags_not_content(client):
    """(d) _meta has validation flags; payload fields are not duplicated inside _meta."""
    with (
        patch("main._db_select", new=AsyncMock(return_value=_CONV_ROW)),
        patch("main._db_insert", new=AsyncMock(return_value=_MSG_ROW)) as mock_insert,
    ):
        resp = client.post("/chat", json=VALID_BODY)

    assert resp.status_code == 200
    content = mock_insert.call_args_list[1].args[1]["content"]
    meta = content["_meta"]

    # Audit flags present
    assert "validation_status" in meta
    assert "repair_applied" in meta
    assert "prompt_version" in meta
    assert "is_mock" in meta

    # Payload fields live in content, not duplicated inside _meta
    assert "questions_to_ask" not in meta
    assert "patient_facing_summary" not in meta
    assert "disclaimer" not in meta

    # Payload fields do exist at the content level
    assert "questions_to_ask" in content
    assert "disclaimer" in content
