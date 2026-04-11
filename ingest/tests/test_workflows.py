"""
Tests for workflow routing and execution (Day 22).

Unit cases (workflows.py in isolation):
  a) Each workflow function returns a schema-valid dict
  b) Correct flag values per workflow
  c) Correct disclaimer per workflow

Integration cases (via /chat endpoint):
  d) Each mode produces a 200 with schema-valid assistant_payload
  e) doc_qa with no retrieval → flag="uncertain" (no-source fallback)
  f) summary with no retrieval → flag="safe" (no-source fallback skipped for summary)
  g) patient_message with no retrieval → flag="safe"
  h) metadata stores workflow_name + router_reason
  i) doc_qa mode accepted by ChatRequest (not rejected as invalid)
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from unittest.mock import AsyncMock, patch

from classifier import ClassifiedInput
from workflows import run_doc_qa, run_patient_message, run_summary, run_triage

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_REQUIRED_FIELDS = (
    "questions_to_ask",
    "red_flags",
    "possible_next_steps",
    "patient_facing_summary",
    "sources",
    "flag",
    "disclaimer",
)

CONV_ID = "conv-00000000-0000-0000-0000-000000000001"
_CONV_ROW = [{"id": CONV_ID}]
_MSG_ROW = {"id": "msg-00000000-0000-0000-0000-000000000001"}

_SAFE_CLASSIFIED = ClassifiedInput(
    sanitized_text="Patient presents with chest pain, duration two hours.",
    input_hash="abc123",
    pii_flags=[],
    injection_flags=[],
    is_vague=False,
    is_unsafe_request=False,
)


def _chat_body(mode: str) -> dict:
    return {
        "mode": mode,
        "input_text": "Patient presents with chest pain, duration two hours.",
        "conversation_id": CONV_ID,
    }


# ---------------------------------------------------------------------------
# a-c) Workflow functions: schema validity, flags, disclaimers
# ---------------------------------------------------------------------------


def _assert_schema_valid(d: dict) -> None:
    for field in _REQUIRED_FIELDS:
        assert field in d, f"Missing field: {field}"
    assert isinstance(d["questions_to_ask"], list)
    assert isinstance(d["red_flags"], list)
    assert isinstance(d["possible_next_steps"], list)
    assert isinstance(d["sources"], list)
    assert d["flag"] in ("safe", "uncertain", "refuse")
    assert d["disclaimer"]


def test_run_triage_schema_valid():
    d = run_triage(_SAFE_CLASSIFIED)
    _assert_schema_valid(d)


def test_run_triage_flag_uncertain():
    d = run_triage(_SAFE_CLASSIFIED)
    assert d["flag"] == "uncertain"


def test_run_triage_has_red_flags():
    d = run_triage(_SAFE_CLASSIFIED)
    assert len(d["red_flags"]) > 0


def test_run_triage_has_questions():
    d = run_triage(_SAFE_CLASSIFIED)
    assert len(d["questions_to_ask"]) > 0


def test_run_summary_schema_valid():
    d = run_summary(_SAFE_CLASSIFIED)
    _assert_schema_valid(d)


def test_run_summary_flag_safe():
    d = run_summary(_SAFE_CLASSIFIED)
    assert d["flag"] == "safe"


def test_run_summary_no_questions():
    d = run_summary(_SAFE_CLASSIFIED)
    assert d["questions_to_ask"] == []


def test_run_patient_message_schema_valid():
    d = run_patient_message(_SAFE_CLASSIFIED)
    _assert_schema_valid(d)


def test_run_patient_message_flag_safe():
    d = run_patient_message(_SAFE_CLASSIFIED)
    assert d["flag"] == "safe"


def test_run_patient_message_has_emergency_instruction():
    d = run_patient_message(_SAFE_CLASSIFIED)
    # Emergency number must appear in steps or disclaimer for patient safety
    combined = " ".join(d["possible_next_steps"]) + d["disclaimer"]
    assert "112" in combined


def test_run_doc_qa_schema_valid():
    d = run_doc_qa(_SAFE_CLASSIFIED)
    _assert_schema_valid(d)


def test_run_doc_qa_flag_safe():
    # run_doc_qa returns 'safe' — precondition: main.py only calls it with sources
    d = run_doc_qa(_SAFE_CLASSIFIED)
    assert d["flag"] == "safe"


def test_workflow_sources_empty_by_default():
    # Sources are injected by main.py, not by workflow functions
    for fn in (run_triage, run_summary, run_patient_message, run_doc_qa):
        assert fn(_SAFE_CLASSIFIED)["sources"] == [], f"{fn.__name__} should return empty sources"


def test_rag_context_parameter_accepted():
    # rag_context is the Week 3 seam — must be accepted without error
    for fn in (run_triage, run_summary, run_patient_message, run_doc_qa):
        fn(_SAFE_CLASSIFIED, rag_context="<retrieved_context>...</retrieved_context>")


# ---------------------------------------------------------------------------
# d-h) Integration via /chat endpoint
# ---------------------------------------------------------------------------


def test_triage_mode_200_schema_valid(client):
    with (
        patch("main._db_select", new=AsyncMock(return_value=_CONV_ROW)),
        patch("main._db_insert", new=AsyncMock(return_value=_MSG_ROW)),
    ):
        resp = client.post("/chat", json=_chat_body("triage"))

    assert resp.status_code == 200
    payload = resp.json()["assistant_payload"]
    for field in _REQUIRED_FIELDS:
        assert field in payload, f"Missing: {field}"


def test_summary_mode_200_schema_valid(client):
    with (
        patch("main._db_select", new=AsyncMock(return_value=_CONV_ROW)),
        patch("main._db_insert", new=AsyncMock(return_value=_MSG_ROW)),
    ):
        resp = client.post("/chat", json=_chat_body("summary"))

    assert resp.status_code == 200
    payload = resp.json()["assistant_payload"]
    for field in _REQUIRED_FIELDS:
        assert field in payload


def test_patient_message_mode_200_schema_valid(client):
    with (
        patch("main._db_select", new=AsyncMock(return_value=_CONV_ROW)),
        patch("main._db_insert", new=AsyncMock(return_value=_MSG_ROW)),
    ):
        resp = client.post("/chat", json=_chat_body("patient_message"))

    assert resp.status_code == 200
    payload = resp.json()["assistant_payload"]
    for field in _REQUIRED_FIELDS:
        assert field in payload


def test_doc_qa_mode_200_schema_valid(client):
    """doc_qa is a valid mode and returns schema-valid output."""
    with (
        patch("main._db_select", new=AsyncMock(return_value=_CONV_ROW)),
        patch("main._db_insert", new=AsyncMock(return_value=_MSG_ROW)),
    ):
        resp = client.post("/chat", json=_chat_body("doc_qa"))

    assert resp.status_code == 200
    payload = resp.json()["assistant_payload"]
    for field in _REQUIRED_FIELDS:
        assert field in payload


def test_doc_qa_no_retrieval_returns_uncertain(client):
    """DOC_QA without high-confidence sources must return flag='uncertain'.

    No embedder in test env → retrieval returns [] → no-source fallback fires.
    """
    with (
        patch("main._db_select", new=AsyncMock(return_value=_CONV_ROW)),
        patch("main._db_insert", new=AsyncMock(return_value=_MSG_ROW)),
    ):
        resp = client.post("/chat", json=_chat_body("doc_qa"))

    assert resp.status_code == 200
    assert resp.json()["assistant_payload"]["flag"] == "uncertain"


def test_summary_no_retrieval_returns_safe(client):
    """Summary does NOT require sources — no-source fallback must NOT fire.

    No embedder in test env → retrieval returns [].
    summary workflow returns flag='safe' and should not be overridden.
    """
    with (
        patch("main._db_select", new=AsyncMock(return_value=_CONV_ROW)),
        patch("main._db_insert", new=AsyncMock(return_value=_MSG_ROW)),
    ):
        resp = client.post("/chat", json=_chat_body("summary"))

    assert resp.status_code == 200
    assert resp.json()["assistant_payload"]["flag"] == "safe"


def test_patient_message_no_retrieval_returns_safe(client):
    """patient_message does NOT require sources — flag must be 'safe'."""
    with (
        patch("main._db_select", new=AsyncMock(return_value=_CONV_ROW)),
        patch("main._db_insert", new=AsyncMock(return_value=_MSG_ROW)),
    ):
        resp = client.post("/chat", json=_chat_body("patient_message"))

    assert resp.status_code == 200
    assert resp.json()["assistant_payload"]["flag"] == "safe"


def test_metadata_stores_workflow_name_and_router_reason(client):
    """Both user and assistant messages store workflow_name + router_reason."""
    with (
        patch("main._db_select", new=AsyncMock(return_value=_CONV_ROW)),
        patch("main._db_insert", new=AsyncMock(return_value=_MSG_ROW)) as mock_insert,
    ):
        client.post("/chat", json=_chat_body("summary"))

    # user message (first insert)
    user_content = mock_insert.call_args_list[0].args[1]["content"]
    assert user_content["_meta"]["workflow_name"] == "summary"
    assert user_content["_meta"]["router_reason"] == "mode_summary"

    # assistant message (second insert)
    assistant_content = mock_insert.call_args_list[1].args[1]["content"]
    assert assistant_content["_meta"]["workflow_name"] == "summary"
    assert assistant_content["_meta"]["router_reason"] == "mode_summary"


def test_response_metadata_includes_workflow(client):
    """ResponseMetadata in the API response includes workflow and router_reason."""
    with (
        patch("main._db_select", new=AsyncMock(return_value=_CONV_ROW)),
        patch("main._db_insert", new=AsyncMock(return_value=_MSG_ROW)),
    ):
        resp = client.post("/chat", json=_chat_body("doc_qa"))

    meta = resp.json()["response_metadata"]
    assert meta["workflow"] == "doc_qa"
    assert meta["router_reason"] == "mode_doc_qa"


def test_invalid_mode_rejected_by_schema(client):
    """Unknown modes are rejected at the Pydantic boundary — 422."""
    with (
        patch("main._db_select", new=AsyncMock(return_value=_CONV_ROW)),
        patch("main._db_insert", new=AsyncMock(return_value=_MSG_ROW)),
    ):
        resp = client.post("/chat", json={**_chat_body("triage"), "mode": "unknown_mode"})

    assert resp.status_code == 422
