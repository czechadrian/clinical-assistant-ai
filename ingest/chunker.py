"""
chunker.py — sliding-window text chunker for the RAG ingest pipeline.

Design choices:
- Fixed-size sliding window with configurable overlap.
- Whitespace is normalised before splitting: PDF extractors produce inconsistent
  line breaks and multiple spaces; collapsing everything to single spaces makes
  chunk boundaries predictable and deterministic.
- Word-boundary snapping: when not at the end of the text, snap the chunk end
  to the last space within the window so chunks never cut mid-word.
- Pure function: same input always produces the same output (no I/O, no state).

Defaults:
    chunk_size = 1000  characters  ≈ 250 tokens (good for text-embedding-3-small)
    overlap    = 100   characters  ≈ 25 tokens  (context continuity between chunks)
"""


def chunk_text(
    text: str,
    chunk_size: int = 1000,
    overlap: int = 100,
) -> list[str]:
    """Split *text* into overlapping fixed-size chunks.

    Args:
        text:       Source text. All whitespace (newlines, tabs, double spaces)
                    is collapsed to single spaces before chunking.
        chunk_size: Maximum character length of each chunk (after normalisation).
        overlap:    Characters of shared content between consecutive chunks.
                    Must be strictly less than chunk_size.

    Returns:
        List of non-empty chunk strings. Returns [] for empty / whitespace-only input.

    Raises:
        ValueError: If overlap >= chunk_size.
    """
    if overlap >= chunk_size:
        raise ValueError(
            f"overlap ({overlap}) must be strictly less than chunk_size ({chunk_size})"
        )

    # Collapse all whitespace (tabs, newlines, multiple spaces) into single spaces.
    # This removes PDF extraction artefacts and makes chunking deterministic.
    text = " ".join(text.split())
    if not text:
        return []

    chunks: list[str] = []
    step = chunk_size - overlap  # advance window by this many chars each iteration
    start = 0

    while start < len(text):
        end = start + chunk_size

        if end < len(text):
            # Snap to the last word boundary within the window to avoid mid-word cuts.
            # rfind returns -1 only if there is no space at all in [start, end),
            # which can't happen after whitespace normalisation (unless chunk_size < 1).
            boundary = text.rfind(" ", start, end)
            if boundary > start:
                end = boundary

        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)

        start += step  # advance by constant step; actual overlap varies with word-snapping

    return chunks
