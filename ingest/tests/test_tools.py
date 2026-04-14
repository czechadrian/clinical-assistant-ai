"""
Tests for tools/guidelines_search.py

Coverage:
  A) Schema validation — GuidelinesSearchInput and GuidelinesSearchOutput
  B) query_hash — stability and privacy properties
  C) run() — unit tests via injected mocks (no FastAPI, no DB)
  D) _should_use_tool — deterministic routing rules
  E) Integration: doc_qa always uses tool and populates sources
  F) Integration: empty tool result triggers no-source uncertainty
  G) Integration: tool NOT called for triage without guideline language
  H) Integration: tool called for triage WITH guideline language
"""

from unittest.mock import AsyncMock, patch

import pytest
from pydantic import ValidationError

from tools.guidelines_search import (
    TOOL_NAME,
    GuidelinesSearchInput,
    GuidelinesSearchOutput,
    ToolChunkItem,
    ToolFilters,
    query_hash,
    run,
)

# ---------------------------------------------------------------------------
# Fixtures shared across integration tests
# ---------------------------------------------------------------------------

_CONV_ROW = [{"id": "conv-00000000-0000-0000-0000-000000000001"}]
_MSG_ROW = {"id": "msg-00000000-0000-0000-0000-000000000001"}

_CHUNK_ROW = {
    "chunk_id": "aaaaaaaa-0000-0000-0000-000000000001",
    "doc_id": "bbbbbbbb-0000-0000-0000-000000000001",
    "title": "PTK Guidelines 2024",
    "section": "HFrEF",
    "content": "Zgodnie z wytycznymi PTK 2024 pierwszą linią leczenia jest ACEI." * 5,
    "score": 0.92,
}

_DOC_QA_BODY = {
    "mode": "doc_qa",
    "input_text": "What is the first-line treatment for HFrEF?",
    "conversation_id": "conv-00000000-0000-0000-0000-000000000001",
}

_TRIAGE_GUIDELINE_BODY = {
    "mode": "triage",
    "input_text": "Proszę ocenić pacjenta na podstawie wytycznych PTK w sprawie OZW.",
    "conversation_id": "conv-00000000-0000-0000-0000-000000000001",
}

_TRIAGE_PLAIN_BODY = {
    "mode": "triage",
    "input_text": "Patient presents with chest pain, duration two hours.",
    "conversation_id": "conv-00000000-0000-0000-0000-000000000001",
}


# ===========================================================================
# A) Schema validation
# ===========================================================================


class TestGuidelinesSearchInput:
    def test_valid_minimal(self):
        inp = GuidelinesSearchInput(query="chest pain")
        assert inp.query == "chest pain"
        assert inp.top_k == 3
        assert inp.filters == ToolFilters()

    def test_valid_full(self):
        inp = GuidelinesSearchInput(
            query="HFrEF treatment",
            top_k=5,
            filters=ToolFilters(doc_id="doc-123", source_type="guideline", year=2024),
        )
        assert inp.top_k == 5
        assert inp.filters.doc_id == "doc-123"

    def test_query_empty_raises(self):
        with pytest.raises(ValidationError):
            GuidelinesSearchInput(query="")

    def test_query_too_long_raises(self):
        with pytest.raises(ValidationError):
            GuidelinesSearchInput(query="x" * 501)

    def test_top_k_zero_raises(self):
        with pytest.raises(ValidationError):
            GuidelinesSearchInput(query="q", top_k=0)

    def test_top_k_eleven_raises(self):
        with pytest.raises(ValidationError):
            GuidelinesSearchInput(query="q", top_k=11)

    def test_top_k_boundaries_valid(self):
        GuidelinesSearchInput(query="q", top_k=1)
        GuidelinesSearchInput(query="q", top_k=10)


class TestGuidelinesSearchOutput:
    def test_empty_output(self):
        out = GuidelinesSearchOutput(items=[])
        assert out.items == []

    def test_item_fields_present(self):
        item = ToolChunkItem(
            chunk_id="c1",
            doc_id="d1",
            title="PTK",
            section="HFrEF",
            score=0.91,
            snippet="text",
        )
        out = GuidelinesSearchOutput(items=[item])
        assert len(out.items) == 1
        assert out.items[0].chunk_id == "c1"


# ===========================================================================
# B) query_hash
# ===========================================================================


class TestQueryHash:
    def test_same_query_same_hash(self):
        assert query_hash("chest pain") == query_hash("chest pain")

    def test_different_query_different_hash(self):
        assert query_hash("chest pain") != query_hash("head ache")

    def test_hash_is_16_hex_chars(self):
        h = query_hash("test")
        assert len(h) == 16
        assert all(c in "0123456789abcdef" for c in h)

    def test_hash_does_not_contain_query(self):
        """The hash must not leak the original query text."""
        h = query_hash("supersecret patient info 12345")
        assert "supersecret" not in h
        assert "patient" not in h


# ===========================================================================
# C) run() — unit tests with injected mocks
# ===========================================================================


@pytest.mark.anyio
async def test_run_returns_empty_when_embedder_returns_nothing():
    """If embedder returns empty list, run() returns empty output gracefully."""
    embed_fn = AsyncMock(return_value=[])
    db_rpc_fn = AsyncMock()  # should never be called

    out = await run(
        GuidelinesSearchInput(query="test"),
        embedder_embed=embed_fn,
        db_rpc=db_rpc_fn,
        jwt="jwt-test",
    )
    assert out.items == []
    db_rpc_fn.assert_not_called()


@pytest.mark.anyio
async def test_run_filters_chunks_below_min_score():
    """Chunks with score < MIN_SCORE (0.5) are excluded from items."""
    embed_fn = AsyncMock(return_value=[[0.1] * 1536])
    db_rpc_fn = AsyncMock(
        return_value=[
            {**_CHUNK_ROW, "score": 0.49},  # below threshold — excluded
            {**_CHUNK_ROW, "chunk_id": "ccc", "score": 0.50},  # at threshold — included
            {**_CHUNK_ROW, "chunk_id": "ddd", "score": 0.91},  # above threshold — included
        ]
    )

    out = await run(
        GuidelinesSearchInput(query="test"),
        embedder_embed=embed_fn,
        db_rpc=db_rpc_fn,
        jwt="jwt-test",
    )
    chunk_ids = [item.chunk_id for item in out.items]
    assert "ccc" in chunk_ids
    assert "ddd" in chunk_ids
    # First row (score=0.49) must be absent
    assert not any(item.score < 0.5 for item in out.items)


@pytest.mark.anyio
async def test_run_maps_row_fields_correctly():
    """Row dict keys map to the expected ToolChunkItem fields."""
    embed_fn = AsyncMock(return_value=[[0.0] * 1536])
    db_rpc_fn = AsyncMock(return_value=[_CHUNK_ROW])

    out = await run(
        GuidelinesSearchInput(query="HFrEF"),
        embedder_embed=embed_fn,
        db_rpc=db_rpc_fn,
        jwt="jwt-test",
    )
    assert len(out.items) == 1
    item = out.items[0]
    assert item.chunk_id == _CHUNK_ROW["chunk_id"]
    assert item.doc_id == _CHUNK_ROW["doc_id"]
    assert item.title == _CHUNK_ROW["title"]
    assert item.section == _CHUNK_ROW["section"]
    assert item.score == _CHUNK_ROW["score"]
    assert item.snippet == _CHUNK_ROW["content"][:300]


@pytest.mark.anyio
async def test_run_snippet_is_truncated_to_300_chars():
    """snippet is at most 300 characters even for long chunk content."""
    long_content = "A" * 600
    embed_fn = AsyncMock(return_value=[[0.0] * 1536])
    db_rpc_fn = AsyncMock(return_value=[{**_CHUNK_ROW, "content": long_content, "score": 0.9}])

    out = await run(
        GuidelinesSearchInput(query="test"),
        embedder_embed=embed_fn,
        db_rpc=db_rpc_fn,
        jwt="jwt-test",
    )
    assert len(out.items[0].snippet) == 300


@pytest.mark.anyio
async def test_run_forwards_jwt_to_db_rpc():
    """db_rpc must be called with the user's JWT — RLS guarantee."""
    embed_fn = AsyncMock(return_value=[[0.0] * 1536])
    db_rpc_fn = AsyncMock(return_value=[])
    user_jwt = "user-specific-jwt-abc"

    await run(
        GuidelinesSearchInput(query="test"),
        embedder_embed=embed_fn,
        db_rpc=db_rpc_fn,
        jwt=user_jwt,
    )
    # Third positional argument to db_rpc must be the user's JWT
    _fn_name, _params, forwarded_jwt = db_rpc_fn.call_args[0]
    assert forwarded_jwt == user_jwt


@pytest.mark.anyio
async def test_run_passes_doc_id_filter_to_rpc():
    """doc_id filter is forwarded to RPC params when set."""
    embed_fn = AsyncMock(return_value=[[0.0] * 1536])
    db_rpc_fn = AsyncMock(return_value=[])

    await run(
        GuidelinesSearchInput(query="test", filters=ToolFilters(doc_id="doc-xyz")),
        embedder_embed=embed_fn,
        db_rpc=db_rpc_fn,
        jwt="jwt",
    )
    _, rpc_params, _ = db_rpc_fn.call_args[0]
    assert rpc_params["filter_doc_id"] == "doc-xyz"


@pytest.mark.anyio
async def test_run_no_filter_does_not_pass_filter_doc_id():
    """When filters.doc_id is None, filter_doc_id must NOT appear in RPC params."""
    embed_fn = AsyncMock(return_value=[[0.0] * 1536])
    db_rpc_fn = AsyncMock(return_value=[])

    await run(
        GuidelinesSearchInput(query="test"),
        embedder_embed=embed_fn,
        db_rpc=db_rpc_fn,
        jwt="jwt",
    )
    _, rpc_params, _ = db_rpc_fn.call_args[0]
    assert "filter_doc_id" not in rpc_params


# ===========================================================================
# D) _should_use_tool — pure routing rules
# ===========================================================================

from main import _should_use_tool  # noqa: E402 (after tool tests for clarity)


class TestShouldUseTool:
    def test_doc_qa_always_true(self):
        assert _should_use_tool("doc_qa", "Any question.") is True
        assert _should_use_tool("doc_qa", "") is True

    def test_triage_with_polish_guideline_phrase(self):
        assert _should_use_tool("triage", "Na podstawie wytycznych PTK oceń pacjenta.") is True

    def test_triage_with_english_guideline_phrase(self):
        assert _should_use_tool("triage", "Assess based on guidelines.") is True

    def test_triage_with_zgodnie_z_wytycznymi(self):
        assert (
            _should_use_tool("triage", "Zgodnie z wytycznymi ESC należy podać ACE inhibitor.")
            is True
        )

    def test_triage_without_guideline_language(self):
        assert _should_use_tool("triage", "Patient has chest pain since 2 hours.") is False

    def test_triage_short_plain(self):
        assert _should_use_tool("triage", "Ból w klatce piersiowej od 2 godzin.") is False

    def test_summary_never(self):
        assert _should_use_tool("summary", "Anything including wytycznych") is False

    def test_patient_message_never(self):
        assert _should_use_tool("patient_message", "Na podstawie wytycznych") is False


# ===========================================================================
# E) Integration: doc_qa uses tool → sources populated
# ===========================================================================


@patch("main._embedder")
@patch("main._db_rpc", new_callable=AsyncMock)
def test_doc_qa_uses_tool_and_populates_sources(mock_rpc, mock_embedder, client):
    """doc_qa always runs GuidelinesSearch; returned chunk_ids appear in sources."""
    mock_embedder.embed = AsyncMock(return_value=[[0.1] * 1536])
    mock_rpc.return_value = [_CHUNK_ROW]

    with (
        patch("main._db_select", new=AsyncMock(return_value=_CONV_ROW)),
        patch("main._db_insert", new=AsyncMock(return_value=_MSG_ROW)),
    ):
        resp = client.post("/chat", json=_DOC_QA_BODY)

    assert resp.status_code == 200
    data = resp.json()
    sources = data["assistant_payload"]["sources"]
    assert len(sources) == 1
    assert sources[0]["id"] == _CHUNK_ROW["chunk_id"]
    assert sources[0]["title"] == _CHUNK_ROW["title"]
    # Tool metadata must be present in response_metadata
    meta = data["response_metadata"]
    assert meta["tool_used"] is True
    assert meta["tool_name"] == TOOL_NAME
    assert _CHUNK_ROW["chunk_id"] in meta["retrieved_chunk_ids"]


# ===========================================================================
# F) Integration: empty tool result → no-source uncertainty
# ===========================================================================


@patch("main._embedder")
@patch("main._db_rpc", new_callable=AsyncMock)
def test_doc_qa_empty_tool_result_triggers_uncertainty(mock_rpc, mock_embedder, client):
    """doc_qa + tool returns 0 items → no-source uncertain payload."""
    mock_embedder.embed = AsyncMock(return_value=[[0.1] * 1536])
    mock_rpc.return_value = []  # nothing in the knowledge base

    with (
        patch("main._db_select", new=AsyncMock(return_value=_CONV_ROW)),
        patch("main._db_insert", new=AsyncMock(return_value=_MSG_ROW)),
    ):
        resp = client.post("/chat", json=_DOC_QA_BODY)

    assert resp.status_code == 200
    payload = resp.json()["assistant_payload"]
    assert payload["flag"] == "uncertain"
    assert payload["sources"] == []
    # At least one question must mention the missing documents
    assert any(
        "dokument" in q.lower() or "wytyczn" in q.lower() or "źródł" in q.lower()
        for q in payload["questions_to_ask"]
    )


# ===========================================================================
# G) Integration: triage without guideline language — tool NOT called
# ===========================================================================


@patch("main._embedder")
@patch("main._db_rpc", new_callable=AsyncMock)
def test_triage_without_guidelines_does_not_call_tool(mock_rpc, mock_embedder, client):
    """Plain triage query (no guideline reference) must not invoke the tool."""
    mock_embedder.embed = AsyncMock(return_value=[[0.1] * 1536])

    with (
        patch("main._db_select", new=AsyncMock(return_value=_CONV_ROW)),
        patch("main._db_insert", new=AsyncMock(return_value=_MSG_ROW)),
    ):
        resp = client.post("/chat", json=_TRIAGE_PLAIN_BODY)

    assert resp.status_code == 200
    # embedder.embed must NOT have been called (no retrieval)
    mock_embedder.embed.assert_not_called()
    # db_rpc must NOT have been called either
    mock_rpc.assert_not_called()
    meta = resp.json()["response_metadata"]
    assert meta["tool_used"] is False
    assert meta["tool_name"] is None


# ===========================================================================
# H) Integration: triage WITH guideline language — tool called
# ===========================================================================


@patch("main._embedder")
@patch("main._db_rpc", new_callable=AsyncMock)
def test_triage_with_guideline_language_calls_tool(mock_rpc, mock_embedder, client):
    """Triage query with 'na podstawie wytycznych' triggers GuidelinesSearch."""
    mock_embedder.embed = AsyncMock(return_value=[[0.1] * 1536])
    mock_rpc.return_value = [_CHUNK_ROW]

    with (
        patch("main._db_select", new=AsyncMock(return_value=_CONV_ROW)),
        patch("main._db_insert", new=AsyncMock(return_value=_MSG_ROW)),
    ):
        resp = client.post("/chat", json=_TRIAGE_GUIDELINE_BODY)

    assert resp.status_code == 200
    mock_embedder.embed.assert_called_once()
    mock_rpc.assert_called_once()
    meta = resp.json()["response_metadata"]
    assert meta["tool_used"] is True
    sources = resp.json()["assistant_payload"]["sources"]
    assert len(sources) == 1
    assert sources[0]["id"] == _CHUNK_ROW["chunk_id"]
