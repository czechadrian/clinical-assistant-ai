"""
Smoke tests for POST /chat.

Four cases:
  a) 200 — valid request with mock mode enabled
  b) 422 — empty input_text (Pydantic min_length=1 rejects at schema level)
  c) 400 — PII detected in input
  d) 501 — CHAT_MOCK_MODE disabled
"""

from unittest.mock import AsyncMock, patch

import main

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

VALID_BODY = {
    "mode": "triage",
    "input_text": "Patient presents with chest pain, duration two hours.",
    "conversation_id": "conv-00000000-0000-0000-0000-000000000001",
}

# _db_select returns a non-empty list → conversation ownership check passes
_CONV_ROW = [{"id": VALID_BODY["conversation_id"]}]
_MSG_ROW = {"id": "msg-00000000-0000-0000-0000-000000000001"}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_chat_200_valid(client):
    """Valid request in mock mode returns the structured assistant payload."""
    with (
        patch("main._db_select", new=AsyncMock(return_value=_CONV_ROW)),
        patch("main._db_insert", new=AsyncMock(return_value=_MSG_ROW)),
    ):
        resp = client.post("/chat", json=VALID_BODY)

    assert resp.status_code == 200
    data = resp.json()
    assert "request_id" in data
    assert "assistant_payload" in data
    payload = data["assistant_payload"]
    # Schema check — all required fields present
    for field in (
        "questions_to_ask",
        "red_flags",
        "possible_next_steps",
        "patient_facing_summary",
        "sources",
        "flag",
        "disclaimer",
    ):
        assert field in payload, f"missing field: {field}"
    assert payload["flag"] in ("safe", "uncertain", "refuse")


def test_chat_422_empty_input(client):
    """Empty input_text is rejected by Pydantic validation (min_length=1) before auth."""
    resp = client.post(
        "/chat",
        json={**VALID_BODY, "input_text": ""},
    )
    assert resp.status_code == 422


def test_chat_400_pii_detected(client):
    """Input containing an e-mail address triggers the PII guard and returns 400."""
    resp = client.post(
        "/chat",
        json={**VALID_BODY, "input_text": "Contact patient at jan.kowalski@example.com"},
    )
    assert resp.status_code == 400
    detail = resp.json()["detail"].lower()
    assert "identifying" in detail or "pii" in detail


def test_chat_501_mock_disabled(client, monkeypatch):
    """When CHAT_MOCK_MODE is false the endpoint returns 501 immediately."""
    monkeypatch.setattr(main.settings, "chat_mock_mode", False)
    resp = client.post("/chat", json=VALID_BODY)
    assert resp.status_code == 501
    assert "CHAT_MOCK_MODE" in resp.json()["detail"]
