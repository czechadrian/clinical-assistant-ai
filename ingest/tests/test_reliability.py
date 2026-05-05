"""
Tests for timeouts, retry, idempotency, and error taxonomy (Day 12).

Three focused test groups:
  1. Retry — _retrying_request retries transient statuses; returns 4xx immediately.
  2. Idempotency — duplicate Idempotency-Key returns cached response with no DB writes.
  3. Error taxonomy — AppError produces {"error": {"code", "message", "request_id"}} JSON.
"""

import asyncio
import os
import sys
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import httpx

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from main import _retrying_request  # noqa: E402

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

CONV_ID = "conv-00000000-0000-0000-0000-000000000001"
_CONV_ROW = [{"id": CONV_ID}]
_MSG_ROW = {"id": "msg-00000000-0000-0000-0000-000000000001"}

VALID_BODY = {
    "mode": "triage",
    "input_text": "Patient presents with chest pain, duration two hours.",
    "conversation_id": CONV_ID,
}

# A valid assistant content row as it would be stored in the DB.
_IDEM_KEY = str(uuid.uuid4())
_CACHED_CONTENT = {
    "questions_to_ask": [],
    "red_flags": [],
    "possible_next_steps": [],
    "patient_facing_summary": "Cached summary from prior request.",
    "sources": [],
    "flag": "safe",
    "disclaimer": "Disclaimer.",
    "_meta": {
        "prompt_version": "1.0.0",
        "model": "mock-v1",
        "is_mock": True,
        "validation_status": "valid",
        "repair_applied": False,
        "idempotency_key": _IDEM_KEY,
    },
}
_CACHED_MSG = [{"id": "msg-cached-001", "content": _CACHED_CONTENT}]


# ---------------------------------------------------------------------------
# 1. Retry — unit tests for _retrying_request
# ---------------------------------------------------------------------------


def test_retry_retries_transient_status():
    """_retrying_request retries 429 up to _MAX_RETRIES times then returns 200."""
    call_count = 0

    async def _run():
        nonlocal call_count

        async def factory():
            nonlocal call_count
            call_count += 1
            resp = MagicMock(spec=httpx.Response)
            # First two calls: transient 429; third call: success
            resp.status_code = 429 if call_count < 3 else 200
            return resp

        with patch("asyncio.sleep", new=AsyncMock()):
            return await _retrying_request(factory)

    result = asyncio.run(_run())
    assert result.status_code == 200
    assert call_count == 3  # 2 transient retries + 1 success


def test_retry_does_not_retry_client_error():
    """_retrying_request returns 400 immediately — client errors are not transient."""
    call_count = 0

    async def _run():
        nonlocal call_count

        async def factory():
            nonlocal call_count
            call_count += 1
            resp = MagicMock(spec=httpx.Response)
            resp.status_code = 400
            return resp

        return await _retrying_request(factory)

    result = asyncio.run(_run())
    assert result.status_code == 400
    assert call_count == 1  # no retry on client error


# ---------------------------------------------------------------------------
# 2. Idempotency
# ---------------------------------------------------------------------------


def test_chat_idempotency_returns_cached_response(client):
    """Duplicate Idempotency-Key returns cached payload with zero DB writes."""
    with (
        patch(
            "main._db_select",
            new=AsyncMock(
                side_effect=[
                    _CONV_ROW,  # call 1: conversation ownership check
                    [],          # call 2: conversation_state load → no prior state
                    _CACHED_MSG,  # call 3: idempotency check → hit
                ]
            ),
        ),
        patch("main._db_insert", new=AsyncMock(return_value=_MSG_ROW)) as mock_insert,
    ):
        resp = client.post(
            "/chat",
            json=VALID_BODY,
            headers={"Idempotency-Key": _IDEM_KEY},
        )

    assert resp.status_code == 200
    payload = resp.json()["assistant_payload"]
    # Cached content is returned verbatim
    assert payload["patient_facing_summary"] == "Cached summary from prior request."
    assert payload["flag"] == "safe"

    # No inserts — duplicate must not touch the DB
    assert mock_insert.call_count == 0, "Duplicate request must not insert any rows"


def test_chat_new_request_stores_idempotency_key(client):
    """First request with Idempotency-Key stores the key in assistant _meta."""
    idem_key = str(uuid.uuid4())
    with (
        patch(
            "main._db_select",
            new=AsyncMock(
                side_effect=[
                    _CONV_ROW,  # call 1: conversation ownership check
                    [],         # call 2: conversation_state load → no prior state
                    [],         # call 3: idempotency check → miss (new request)
                ]
            ),
        ),
        patch("main._db_insert", new=AsyncMock(return_value=_MSG_ROW)) as mock_insert,
    ):
        resp = client.post(
            "/chat",
            json=VALID_BODY,
            headers={"Idempotency-Key": idem_key},
        )

    assert resp.status_code == 200
    # Assistant message _meta must contain the idempotency key
    assistant_row = mock_insert.call_args_list[1].args[1]
    assert assistant_row["content"]["_meta"]["idempotency_key"] == idem_key


# ---------------------------------------------------------------------------
# 3. Error taxonomy
# ---------------------------------------------------------------------------


def test_pii_error_shape(client):
    """PII detection returns the stable error JSON shape with code=PII_DETECTED."""
    resp = client.post(
        "/chat",
        json={**VALID_BODY, "input_text": "Patient email: jan.kowalski@example.com"},
    )
    assert resp.status_code == 400
    error = resp.json()["error"]
    assert error["code"] == "PII_DETECTED"
    assert "message" in error
    assert "request_id" in error
    # request_id must be a valid UUID string
    uuid.UUID(error["request_id"])


def test_validation_failed_error_shape(client):
    """Unrepairable payload returns code=VALIDATION_FAILED with no raw values."""
    from tests.test_validator import UNREPAIRABLE_RAW

    with (
        patch("main._db_select", new=AsyncMock(return_value=_CONV_ROW)),
        patch("main._db_insert", new=AsyncMock(return_value=_MSG_ROW)),
        patch("main._dispatch_workflow", return_value=UNREPAIRABLE_RAW),
    ):
        resp = client.post("/chat", json=VALID_BODY)

    assert resp.status_code == 500
    error = resp.json()["error"]
    assert error["code"] == "VALIDATION_FAILED"
    assert "request_id" in error
    # The raw invalid integer value must NOT appear in the error message
    assert "42" not in error["message"]
