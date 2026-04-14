"""
GuidelinesSearch — the only tool available to the chat pipeline on Day 23.

Contract
--------
Input:  GuidelinesSearchInput  { query, top_k, filters }
Output: GuidelinesSearchOutput { items: [ToolChunkItem] }

Hard constraints (enforced by implementation, not policy):
  - Never calls external APIs or websites.
  - RLS is preserved: db_rpc is called with the user-supplied jwt.
  - embedder_embed and db_rpc are injected — this module owns no singletons.
  - query text and snippet content are NEVER logged; only chunk_ids and scores.

Usage (from main.py):
    from tools import guidelines_search
    from tools.guidelines_search import GuidelinesSearchInput, GuidelinesSearchOutput

    output = await guidelines_search.run(
        GuidelinesSearchInput(query=body.input_text, top_k=3),
        embedder_embed=_embedder.embed,
        db_rpc=_db_rpc,
        jwt=auth.jwt,
    )

Filtering: ToolFilters fields (doc_id, source_type, year) are reserved for
future RPC-level filtering once the match_chunks function supports them.
Currently only doc_id is forwarded if set; the others are silently ignored
until the DB function is extended.
"""

from __future__ import annotations

import hashlib
from collections.abc import Awaitable, Callable
from typing import Any

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TOOL_NAME = "GuidelinesSearch"

# Minimum cosine-similarity score to include a chunk.
# Must stay in sync with _MIN_RETRIEVAL_SCORE in main.py.
MIN_SCORE: float = 0.5

# Characters of chunk content to expose as snippet.
_SNIPPET_LEN: int = 300

# ---------------------------------------------------------------------------
# Input / Output schemas
# ---------------------------------------------------------------------------


class ToolFilters(BaseModel):
    """Optional retrieval filters. All fields default to None (no filter applied)."""

    doc_id: str | None = None
    source_type: str | None = None  # reserved: future docs.source_type column
    year: int | None = None  # reserved: future docs.year column


class GuidelinesSearchInput(BaseModel):
    """Validated input for GuidelinesSearch.

    query   — the user's clinical question; 1–500 chars.
    top_k   — maximum chunks to retrieve before score filtering; 1–10.
    filters — optional narrowing filters; defaults to no-filter.
    """

    query: str = Field(..., min_length=1, max_length=500)
    top_k: int = Field(default=3, ge=1, le=10)
    filters: ToolFilters = Field(default_factory=ToolFilters)


class ToolChunkItem(BaseModel):
    """One retrieved chunk in the tool output.

    snippet is the first _SNIPPET_LEN characters of chunk content.
    It is returned to the caller for source display and RAG context assembly.
    It must never be written to logs or persisted beyond what lives in doc_chunks.
    """

    chunk_id: str
    doc_id: str
    title: str
    section: str
    score: float
    snippet: str


class GuidelinesSearchOutput(BaseModel):
    """Tool output: a list of retrieved chunks, filtered by MIN_SCORE."""

    items: list[ToolChunkItem]


# ---------------------------------------------------------------------------
# Dependency type aliases
# ---------------------------------------------------------------------------

# Type of _embedder.embed (OpenAIEmbedder) — async, takes list[str], returns list[list[float]]
EmbedFn = Callable[[list[str]], Awaitable[list[list[float]]]]

# Type of _db_rpc in main.py — async, takes (function_name, params, jwt)
DbRpcFn = Callable[[str, dict, str], Awaitable[list[dict]]]

# ---------------------------------------------------------------------------
# Core execution
# ---------------------------------------------------------------------------


async def run(
    tool_input: GuidelinesSearchInput,
    *,
    embedder_embed: EmbedFn,
    db_rpc: DbRpcFn,
    jwt: str,
) -> GuidelinesSearchOutput:
    """Execute GuidelinesSearch and return schema-validated output.

    Returns GuidelinesSearchOutput(items=[]) on any failure so the caller
    can degrade gracefully without crashing the chat pipeline.

    RLS guarantee: jwt is forwarded to db_rpc unchanged, so PostgREST
    evaluates access policies as the requesting user — no privilege escalation.

    Privacy guarantee: query text and snippet content are not logged here.
    Callers log only query_hash() and [item.chunk_id for item in output.items].
    """
    embeddings = await embedder_embed([tool_input.query])
    if not embeddings:
        return GuidelinesSearchOutput(items=[])

    rpc_params: dict[str, Any] = {
        "query_embedding": embeddings[0],
        "match_count": tool_input.top_k,
    }
    # Forward doc_id filter if set; others reserved for future RPC support.
    if tool_input.filters.doc_id:
        rpc_params["filter_doc_id"] = tool_input.filters.doc_id

    rows = await db_rpc("match_chunks", rpc_params, jwt)

    items = [
        ToolChunkItem(
            chunk_id=str(r["chunk_id"]),
            doc_id=str(r["doc_id"]),
            title=r["title"],
            section=r.get("section") or "",
            score=float(r.get("score", 0)),
            snippet=r["content"][:_SNIPPET_LEN],
        )
        for r in rows
        if float(r.get("score", 0)) >= MIN_SCORE
    ]
    return GuidelinesSearchOutput(items=items)


# ---------------------------------------------------------------------------
# Metadata helper
# ---------------------------------------------------------------------------


def query_hash(query: str) -> str:
    """Return the first 16 hex characters of SHA-256(query).

    Used in message._meta as a privacy-safe reference to the query text.
    Short enough to log; sufficient to detect duplicate queries in audit trails.
    Never use this to reconstruct the original query.
    """
    return hashlib.sha256(query.encode()).hexdigest()[:16]
