"""
Tests for ingest automation endpoints (Day 20).

Endpoint cases:
  a) POST /admin/ingest — 403 for non-admin
  b) POST /admin/ingest — 409 when a doc is already processing (concurrency guard)
  c) POST /admin/ingest — 200 with queued=0 when no pending docs
  d) POST /admin/ingest — 202 when pending docs exist (background task queued)
  e) POST /admin/ingest — 503 when OPENAI_API_KEY not configured
  f) GET  /admin/ingest/status — 403 for non-admin
  g) GET  /admin/ingest/status — counts by status
  h) POST /admin/ingest/reset-stuck — 403 for non-admin
  i) POST /admin/ingest/reset-stuck — 200 with count=0 when no stuck docs
  j) POST /admin/ingest/reset-stuck — resets N stuck docs

Worker unit cases (via asyncio.run):
  k) process_doc sets status='processing' first, then 'ready' on success
  l) process_doc sets status='failed' + last_error_code on extraction failure
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

CONV_ID = "conv-00000000-0000-0000-0000-000000000001"
_ADMIN_PROFILE = [{"role": "admin"}]
_DOCTOR_PROFILE = [{"role": "doctor"}]

_DOC_PENDING = [
    {"id": "doc-00000000-0000-0000-0000-000000000001"},
    {"id": "doc-00000000-0000-0000-0000-000000000002"},
]
_DOC_PROCESSING = [{"id": "doc-00000000-0000-0000-0000-000000000003"}]


def _make_select_mock(profile=None, processing=None, pending=None, status_rows=None):
    """Build an AsyncMock side_effect for _db_select covering multiple queries."""
    _profile = profile if profile is not None else _ADMIN_PROFILE
    _processing = processing if processing is not None else []
    _pending = pending if pending is not None else []
    _status_rows = status_rows or []

    async def _side_effect(path: str, params: dict, jwt: str):
        if "profiles" in path:
            return _profile
        if params.get("status") == "eq.processing":
            return _processing
        if params.get("status") == "eq.pending":
            return _pending
        # GET /admin/ingest/status — returns all docs with their status
        return _status_rows

    return _side_effect


# ---------------------------------------------------------------------------
# a) Non-admin → 403
# ---------------------------------------------------------------------------


def test_trigger_ingest_403_non_admin(client):
    with patch("main._db_select", side_effect=_make_select_mock(profile=_DOCTOR_PROFILE)):
        resp = client.post("/admin/ingest")

    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "FORBIDDEN"


# ---------------------------------------------------------------------------
# b) Already processing → 409
# ---------------------------------------------------------------------------


def test_trigger_ingest_409_already_running(client):
    with (
        patch(
            "main._db_select",
            side_effect=_make_select_mock(processing=_DOC_PROCESSING),
        ),
        patch("main.settings") as mock_settings,
    ):
        mock_settings.openai_api_key = "sk-test"
        resp = client.post("/admin/ingest")

    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "INGEST_RUNNING"


# ---------------------------------------------------------------------------
# c) No pending docs → 200 queued=0
# ---------------------------------------------------------------------------


def test_trigger_ingest_200_no_pending(client):
    with (
        patch(
            "main._db_select",
            side_effect=_make_select_mock(pending=[]),
        ),
        patch("main.settings") as mock_settings,
    ):
        mock_settings.openai_api_key = "sk-test"
        resp = client.post("/admin/ingest")

    assert resp.status_code == 202
    data = resp.json()
    assert data["queued"] == 0


# ---------------------------------------------------------------------------
# d) Pending docs present → 202, background task queued
# ---------------------------------------------------------------------------


def test_trigger_ingest_202_queued(client):
    with (
        patch("main._db_select", side_effect=_make_select_mock(pending=_DOC_PENDING)),
        patch("main._run_ingest_background", new=AsyncMock()),
        patch("main.settings") as mock_settings,
    ):
        mock_settings.openai_api_key = "sk-test"
        resp = client.post("/admin/ingest")

    assert resp.status_code == 202
    data = resp.json()
    assert data["queued"] == 2
    assert "2" in data["message"]


# ---------------------------------------------------------------------------
# e) OPENAI_API_KEY not set → 503
# ---------------------------------------------------------------------------


def test_trigger_ingest_503_no_openai_key(client):
    with (
        patch("main._db_select", side_effect=_make_select_mock()),
        patch("main.settings") as mock_settings,
    ):
        mock_settings.openai_api_key = ""
        resp = client.post("/admin/ingest")

    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "CONFIGURATION_ERROR"


# ---------------------------------------------------------------------------
# f) GET /admin/ingest/status → 403 for non-admin
# ---------------------------------------------------------------------------


def test_ingest_status_403_non_admin(client):
    with patch("main._db_select", side_effect=_make_select_mock(profile=_DOCTOR_PROFILE)):
        resp = client.get("/admin/ingest/status")

    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# g) GET /admin/ingest/status → counts by status
# ---------------------------------------------------------------------------


def test_ingest_status_counts(client):
    status_rows = [
        {"status": "ready"},
        {"status": "ready"},
        {"status": "pending"},
        {"status": "failed"},
    ]
    with patch(
        "main._db_select",
        side_effect=_make_select_mock(status_rows=status_rows),
    ):
        resp = client.get("/admin/ingest/status")

    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 4
    assert data["counts"]["ready"] == 2
    assert data["counts"]["pending"] == 1
    assert data["counts"]["failed"] == 1


# ---------------------------------------------------------------------------
# h) POST /admin/ingest/reset-stuck → 403 for non-admin
# ---------------------------------------------------------------------------


def test_reset_stuck_403_non_admin(client):
    with patch("main._db_select", side_effect=_make_select_mock(profile=_DOCTOR_PROFILE)):
        resp = client.post("/admin/ingest/reset-stuck")

    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# i) POST /admin/ingest/reset-stuck → no stuck docs
# ---------------------------------------------------------------------------


def test_reset_stuck_200_none_stuck(client):
    with patch("main._db_select", side_effect=_make_select_mock(processing=[])):
        resp = client.post("/admin/ingest/reset-stuck")

    assert resp.status_code == 200
    assert resp.json()["queued"] == 0


# ---------------------------------------------------------------------------
# j) POST /admin/ingest/reset-stuck → resets N stuck docs
# ---------------------------------------------------------------------------


def test_reset_stuck_patches_processing_docs(client):
    with (
        patch("main._db_select", side_effect=_make_select_mock(processing=_DOC_PROCESSING)),
        patch("main._db_patch_service_role", new=AsyncMock()),
    ):
        resp = client.post("/admin/ingest/reset-stuck")

    assert resp.status_code == 200
    data = resp.json()
    assert data["queued"] == 1
    assert "1" in data["message"]


# ---------------------------------------------------------------------------
# k) Worker: process_doc transitions pending → processing → ready
# ---------------------------------------------------------------------------


def test_process_doc_transitions_processing_then_ready():
    """Verify status is set to 'processing' before download, 'ready' on success."""
    from worker import process_doc

    doc = {
        "id": "doc-aaa",
        "filename": "test.txt",
        "storage_path": "test.txt",
        "file_hash": "abc123",
        "status": "pending",
        "version": "1",
        "last_ingested_at": None,
        "last_error_code": None,
    }

    patch_calls: list[dict] = []

    async def mock_patch(client, path, params, payload, settings):
        patch_calls.append({"params": params, "payload": payload})

    async def mock_storage(client, storage_path, settings):
        return b"Hello world text content for testing chunking."

    async def mock_post(client, path, payload, settings):
        pass

    async def mock_delete(client, path, params, settings):
        pass

    fake_embedder = MagicMock()
    fake_embedder.embed = AsyncMock(return_value=[[0.1] * 1536])

    fake_settings = MagicMock()

    with (
        patch("worker._db_patch", side_effect=mock_patch),
        patch("worker._storage_download", side_effect=mock_storage),
        patch("worker._db_post", side_effect=mock_post),
        patch("worker._db_delete", side_effect=mock_delete),
    ):
        asyncio.run(process_doc(doc, MagicMock(), fake_embedder, fake_settings))

    statuses = [c["payload"].get("status") for c in patch_calls]
    assert statuses[0] == "processing", f"First status update must be 'processing', got {statuses}"
    assert statuses[-1] == "ready", f"Final status update must be 'ready', got {statuses}"
    # last_ingested_at must be set on success
    assert patch_calls[-1]["payload"].get("last_ingested_at") is not None


# ---------------------------------------------------------------------------
# l) Worker: process_doc transitions processing → failed on error
# ---------------------------------------------------------------------------


def test_process_doc_sets_failed_on_error():
    """Verify status='failed' + last_error_code set when extraction raises."""
    from worker import process_doc

    doc = {
        "id": "doc-bbb",
        "filename": "bad.pdf",
        "storage_path": "bad.pdf",
        "file_hash": "def456",
        "status": "pending",
        "version": "1",
        "last_ingested_at": None,
        "last_error_code": None,
    }

    patch_calls: list[dict] = []

    async def mock_patch(client, path, params, payload, settings):
        patch_calls.append({"params": params, "payload": payload})

    async def mock_storage_fail(client, storage_path, settings):
        raise ValueError("Simulated download failure")

    fake_embedder = MagicMock()
    fake_settings = MagicMock()

    with (
        patch("worker._db_patch", side_effect=mock_patch),
        patch("worker._storage_download", side_effect=mock_storage_fail),
    ):
        with pytest.raises(ValueError):
            asyncio.run(process_doc(doc, MagicMock(), fake_embedder, fake_settings))

    statuses = [c["payload"].get("status") for c in patch_calls]
    assert statuses[0] == "processing"
    assert statuses[-1] == "failed"
    assert patch_calls[-1]["payload"].get("last_error_code") == "Simulated download failure"
