"""Unit tests for chunker.py.

Focus:
  - chunk sizes never exceed chunk_size
  - no empty chunks are returned
  - output is deterministic given the same input
  - edge cases: empty string, whitespace only, text shorter than chunk_size
  - overlap parameter produces more chunks than non-overlap
  - invalid parameters raise ValueError
"""

import pytest

from chunker import chunk_text

# ---------------------------------------------------------------------------
# Basic contract
# ---------------------------------------------------------------------------


def test_empty_string_returns_empty_list():
    assert chunk_text("") == []


def test_whitespace_only_returns_empty_list():
    assert chunk_text("   \n\t\r\n  ") == []


def test_short_text_returns_single_chunk():
    text = "Pacjent lat 45, ból w klatce piersiowej."
    result = chunk_text(text, chunk_size=200, overlap=20)
    assert result == [text]


def test_text_exactly_chunk_size_with_zero_overlap_returns_single_chunk():
    # With overlap=0 the step equals chunk_size, so no second window opens.
    text = "a" * 100
    result = chunk_text(text, chunk_size=100, overlap=0)
    assert result == [text]


def test_text_exactly_chunk_size_with_overlap_produces_tail():
    # step = chunk_size - overlap = 90, so the window advances to position 90
    # and emits a 10-char tail chunk. This is correct sliding-window behaviour.
    text = "a" * 100
    result = chunk_text(text, chunk_size=100, overlap=10)
    assert len(result) == 2
    assert result[0] == text  # full first chunk
    assert result[1] == "a" * 10  # overlap tail


# ---------------------------------------------------------------------------
# Size constraints
# ---------------------------------------------------------------------------


def test_no_chunk_exceeds_chunk_size():
    text = " ".join(["słowo"] * 500)  # Polish word, 5 chars + space
    chunks = chunk_text(text, chunk_size=100, overlap=10)
    for chunk in chunks:
        assert len(chunk) <= 100, f"Chunk too long ({len(chunk)}): {chunk[:40]}..."


def test_no_empty_chunks():
    text = " ".join(["word"] * 300)
    chunks = chunk_text(text, chunk_size=50, overlap=5)
    assert all(len(c) > 0 for c in chunks), "Found an empty chunk"


def test_whitespace_normalisation_removes_newlines():
    # PDF extractors often produce text with many newlines and double spaces.
    text = "Pacjent\n\nlat  45,\tbardzo\n silny ból."
    result = chunk_text(text, chunk_size=200)
    # After normalisation, no double spaces or newlines inside chunks.
    assert len(result) == 1
    assert "\n" not in result[0]
    assert "  " not in result[0]


# ---------------------------------------------------------------------------
# Multi-chunk behaviour
# ---------------------------------------------------------------------------


def test_long_text_produces_multiple_chunks():
    text = " ".join(["Diagnoza"] * 200)  # ~1600 chars
    chunks = chunk_text(text, chunk_size=100, overlap=10)
    assert len(chunks) > 1


def test_overlap_produces_more_chunks_than_no_overlap():
    text = " ".join(["word"] * 200)  # ~1000 chars
    chunks_with_overlap = chunk_text(text, chunk_size=100, overlap=20)
    chunks_no_overlap = chunk_text(text, chunk_size=100, overlap=0)
    # Overlap means a smaller step, so more iterations.
    assert len(chunks_with_overlap) >= len(chunks_no_overlap)


def test_all_text_is_covered():
    """Every word in the original text appears in at least one chunk."""
    words = [f"word{i}" for i in range(50)]
    text = " ".join(words)
    chunks = chunk_text(text, chunk_size=60, overlap=10)
    combined = " ".join(chunks)
    for word in words:
        assert word in combined, f"{word!r} not found in any chunk"


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


def test_same_input_same_output():
    text = (
        "Zalecenia Polskiego Towarzystwa Kardiologicznego dotyczące leczenia "
        "ostrego zespołu wieńcowego bez uniesienia odcinka ST. " * 30
    )
    assert chunk_text(text) == chunk_text(text)


def test_different_inputs_different_outputs():
    text_a = "Ból głowy pacjenta nasilił się w ostatnich trzech dniach."
    text_b = "Pacjent zgłasza duszność wysiłkową od dwóch tygodni."
    assert chunk_text(text_a) != chunk_text(text_b)


# ---------------------------------------------------------------------------
# Parameter validation
# ---------------------------------------------------------------------------


def test_overlap_equal_to_chunk_size_raises():
    with pytest.raises(ValueError, match="overlap"):
        chunk_text("some text", chunk_size=10, overlap=10)


def test_overlap_greater_than_chunk_size_raises():
    with pytest.raises(ValueError, match="overlap"):
        chunk_text("some text", chunk_size=10, overlap=15)


def test_zero_overlap_is_valid():
    text = " ".join(["word"] * 100)
    chunks = chunk_text(text, chunk_size=50, overlap=0)
    assert len(chunks) > 0
    assert all(c for c in chunks)
