"""
Tests for /retrieve endpoint and RAG helpers.

Strategy:
  - _embedder is None in the test environment (OPENAI_API_KEY="") so
    _retrieve_context always returns [] unless patched explicitly.
  - Tests that exercise the real path mock main._embedder and main._db_rpc
    with AsyncMock, the same pattern used for _db_select/_db_insert.
  - No raw chunk content appears in any log output.
"""

import logging
from unittest.mock import AsyncMock, patch

import pytest

from constants import SNIPPET_LEN as _SNIPPET_LEN
from main import _format_rag_context, _retrieve_context

# ---------------------------------------------------------------------------
# _format_rag_context — unit tests (pure function, no I/O)
# ---------------------------------------------------------------------------


def test_format_rag_context_empty_returns_empty_string():
    assert _format_rag_context([]) == ""


def test_format_rag_context_wraps_in_retrieved_context_tags():
    rows = [
        {"doc_id": "d1", "title": "PTK 2024", "section": None, "content": "chunk text"},
    ]
    result = _format_rag_context(rows)
    assert result.startswith("<retrieved_context>")
    assert result.strip().endswith("</retrieved_context>")


def test_format_rag_context_includes_source_tags():
    rows = [
        {"doc_id": "d1", "title": "Guidelines", "section": "Chapter 3", "content": "text"},
    ]
    result = _format_rag_context(rows)
    assert "<source" in result
    assert "doc_id='d1'" in result
    assert "title='Guidelines'" in result
    assert "section='Chapter 3'" in result


def test_format_rag_context_multiple_sources():
    rows = [
        {"doc_id": "d1", "title": "A", "section": None, "content": "chunk 1"},
        {"doc_id": "d2", "title": "B", "section": None, "content": "chunk 2"},
    ]
    result = _format_rag_context(rows)
    assert result.count("<source") == 2


def test_format_rag_context_contains_full_content():
    """Full content must be present — this is what goes to the LLM."""
    rows = [{"doc_id": "d1", "title": "T", "section": None, "content": "full chunk text here"}]
    result = _format_rag_context(rows)
    assert "full chunk text here" in result


# ---------------------------------------------------------------------------
# _retrieve_context — unit tests
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_retrieve_context_returns_empty_when_embedder_is_none():
    """Default test environment has no OPENAI_API_KEY → _embedder is None."""
    rows = await _retrieve_context("query text", top_k=5, jwt="fake-jwt")
    assert rows == []


@pytest.mark.anyio
@patch("main._embedder")
@patch("main._db_rpc", new_callable=AsyncMock)
async def test_retrieve_context_calls_match_chunks(mock_rpc, mock_embedder):
    mock_embedder.embed = AsyncMock(return_value=[[0.1] * 1536])
    mock_rpc.return_value = [
        {
            "chunk_id": "c1",
            "doc_id": "d1",
            "title": "PTK 2024",
            "section": None,
            "content": "some clinical text",
            "score": 0.91,
        }
    ]

    rows = await _retrieve_context("ból klatki piersiowej", top_k=3, jwt="fake-jwt")

    mock_embedder.embed.assert_awaited_once_with(["ból klatki piersiowej"])
    mock_rpc.assert_awaited_once_with(
        "match_chunks",
        {"query_embedding": [0.1] * 1536, "match_count": 3},
        "fake-jwt",
    )
    assert len(rows) == 1
    assert rows[0]["score"] == 0.91


@pytest.mark.anyio
@patch("main._embedder")
async def test_retrieve_context_returns_empty_on_embedder_exception(mock_embedder):
    mock_embedder.embed = AsyncMock(side_effect=RuntimeError("API down"))

    rows = await _retrieve_context("query", top_k=5, jwt="fake-jwt")
    assert rows == []


@pytest.mark.anyio
@patch("main._embedder")
async def test_retrieve_context_logs_warning_not_query_on_error(mock_embedder, caplog):
    """Verify that the query text (patient input) is never logged on failure."""
    mock_embedder.embed = AsyncMock(side_effect=RuntimeError("API down"))
    sensitive_query = "Jan Kowalski PESEL 85010112345 has chest pain"

    with caplog.at_level(logging.WARNING, logger="main"):
        await _retrieve_context(sensitive_query, top_k=5, jwt="fake-jwt")

    # The sensitive query must not appear in any log record.
    for record in caplog.records:
        assert sensitive_query not in record.getMessage()
        assert "85010112345" not in record.getMessage()
        assert "Jan Kowalski" not in record.getMessage()


# ---------------------------------------------------------------------------
# GET /retrieve — integration tests via TestClient
# ---------------------------------------------------------------------------


def test_retrieve_empty_query_returns_empty_without_calling_embedder(client):
    resp = client.get("/retrieve", params={"query": "", "top_k": 3})
    assert resp.status_code == 200
    assert resp.json() == {"items": [], "top_k": 3}


def test_retrieve_whitespace_query_returns_empty(client):
    resp = client.get("/retrieve", params={"query": "   ", "top_k": 3})
    assert resp.status_code == 200
    assert resp.json()["items"] == []


def test_retrieve_top_k_too_large_returns_422(client):
    resp = client.get("/retrieve", params={"query": "test", "top_k": 25})
    assert resp.status_code == 422


def test_retrieve_top_k_zero_returns_422(client):
    resp = client.get("/retrieve", params={"query": "test", "top_k": 0})
    assert resp.status_code == 422


def test_retrieve_no_embedder_configured_returns_503(client):
    """Default test env has OPENAI_API_KEY="" so _embedder is None → 503."""
    resp = client.get("/retrieve", params={"query": "ból głowy", "top_k": 5})
    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "CONFIGURATION_ERROR"


@patch("main._embedder")
@patch("main._db_rpc", new_callable=AsyncMock)
def test_retrieve_returns_items_with_correct_shape(mock_rpc, mock_embedder, client):
    mock_embedder.embed = AsyncMock(return_value=[[0.1] * 1536])
    mock_rpc.return_value = [
        {
            "chunk_id": "aaaaaaaa-0000-0000-0000-000000000001",
            "doc_id": "bbbbbbbb-0000-0000-0000-000000000001",
            "title": "PTK Guidelines 2024",
            "section": "Cardiac",
            "content": "X" * 500,  # longer than _SNIPPET_LEN
            "score": 0.87,
        }
    ]

    resp = client.get("/retrieve", params={"query": "kardiologia", "top_k": 3})
    assert resp.status_code == 200
    data = resp.json()
    assert data["top_k"] == 3
    assert len(data["items"]) == 1

    item = data["items"][0]
    assert item["chunk_id"] == "aaaaaaaa-0000-0000-0000-000000000001"
    assert item["doc_id"] == "bbbbbbbb-0000-0000-0000-000000000001"
    assert item["title"] == "PTK Guidelines 2024"
    assert item["section"] == "Cardiac"
    assert item["score"] == pytest.approx(0.87)
    # text_snippet must be truncated to _SNIPPET_LEN — full content must not leak.
    assert len(item["text_snippet"]) <= _SNIPPET_LEN


@patch("main._embedder")
@patch("main._db_rpc", new_callable=AsyncMock)
def test_retrieve_text_snippet_does_not_expose_full_content(mock_rpc, mock_embedder, client):
    full_content = "A" * 1000
    mock_embedder.embed = AsyncMock(return_value=[[0.1] * 1536])
    mock_rpc.return_value = [
        {
            "chunk_id": "c1",
            "doc_id": "d1",
            "title": "T",
            "section": None,
            "content": full_content,
            "score": 0.9,
        }
    ]

    resp = client.get("/retrieve", params={"query": "test", "top_k": 1})
    snippet = resp.json()["items"][0]["text_snippet"]
    assert len(snippet) == _SNIPPET_LEN
    assert snippet == full_content[:_SNIPPET_LEN]
