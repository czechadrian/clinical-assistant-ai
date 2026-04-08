"""
metrics.py — pure metric computation for RAG evaluation.

No I/O, no side effects — safe to import and test in isolation.

Metric definitions
------------------
hit_rate_at_k
    Fraction of answerable questions where ≥1 retrieved chunk matches
    (by doc_id if expected_doc_id is set, otherwise by keyword substring in title).

mrr
    Mean Reciprocal Rank for answerable questions.
    MRR = mean(1 / rank_of_first_match); 0.0 if no match.

empty_retrieval_rate
    Fraction of unanswerable questions that returned 0 chunks.
    A high value means the system correctly refuses to fabricate sources.

source_correctness_rate
    Subset of answerable questions that have expected_doc_id set; fraction
    where at least one retrieved chunk's doc_id matches exactly.
    Returns None when no questions in the subset have expected_doc_id set.
"""

from typing import TypedDict


class RetrievalResult(TypedDict):
    """One evaluated question with its retrieval output."""

    question_id: str
    answerable: bool
    expected_doc_id: str | None
    expected_doc_keywords: list[str]
    retrieved: list[dict]  # each dict has at minimum: doc_id, title, score


# ---------------------------------------------------------------------------
# Primitive helpers
# ---------------------------------------------------------------------------


def keyword_hit(retrieved: list[dict], keywords: list[str]) -> bool:
    """Return True if any retrieved chunk's title contains any keyword (case-insensitive)."""
    if not keywords or not retrieved:
        return False
    lower_keywords = [kw.lower() for kw in keywords]
    for chunk in retrieved:
        title = (chunk.get("title") or "").lower()
        if any(kw in title for kw in lower_keywords):
            return True
    return False


def doc_id_hit(retrieved: list[dict], expected_doc_id: str) -> bool:
    """Return True if any retrieved chunk belongs to expected_doc_id."""
    return any(str(chunk.get("doc_id", "")) == expected_doc_id for chunk in retrieved)


def first_hit_rank(
    retrieved: list[dict],
    expected_doc_id: str | None,
    expected_doc_keywords: list[str],
) -> int:
    """Return 1-based rank of first matching chunk, or 0 if no match found."""
    lower_keywords = [kw.lower() for kw in expected_doc_keywords]
    for rank, chunk in enumerate(retrieved, start=1):
        if expected_doc_id and str(chunk.get("doc_id", "")) == expected_doc_id:
            return rank
        title = (chunk.get("title") or "").lower()
        if lower_keywords and any(kw in title for kw in lower_keywords):
            return rank
    return 0


def is_hit(result: RetrievalResult) -> bool:
    """Return True if the result has a matching chunk for an answerable question."""
    if result["expected_doc_id"]:
        return doc_id_hit(result["retrieved"], result["expected_doc_id"])
    return keyword_hit(result["retrieved"], result["expected_doc_keywords"])


# ---------------------------------------------------------------------------
# Aggregate metrics
# ---------------------------------------------------------------------------


def compute_metrics(results: list[RetrievalResult]) -> dict:
    """Compute aggregate evaluation metrics over all results.

    Returns a dict with:
      hit_rate_at_k          — 0.0–1.0, answerable questions only
      mrr                    — 0.0–1.0, answerable questions only
      empty_retrieval_rate   — 0.0–1.0, unanswerable questions only
      source_correctness_rate — 0.0–1.0 or None (answerable with expected_doc_id set)
      answerable_count       — int
      unanswerable_count     — int
      total                  — int
    """
    answerable = [r for r in results if r["answerable"]]
    unanswerable = [r for r in results if not r["answerable"]]

    # hit_rate@k and MRR — answerable subset
    hit_count = 0
    reciprocal_rank_sum = 0.0
    for r in answerable:
        rank = first_hit_rank(r["retrieved"], r["expected_doc_id"], r["expected_doc_keywords"])
        if rank > 0:
            hit_count += 1
            reciprocal_rank_sum += 1.0 / rank

    hit_rate = hit_count / len(answerable) if answerable else 0.0
    mrr = reciprocal_rank_sum / len(answerable) if answerable else 0.0

    # empty_retrieval_rate — unanswerable subset
    empty_count = sum(1 for r in unanswerable if not r["retrieved"])
    empty_retrieval_rate = empty_count / len(unanswerable) if unanswerable else 0.0

    # source_correctness_rate — answerable questions with expected_doc_id set
    with_doc_id = [r for r in answerable if r["expected_doc_id"]]
    if with_doc_id:
        correct_count = sum(
            1 for r in with_doc_id if doc_id_hit(r["retrieved"], r["expected_doc_id"])
        )  # type: ignore[arg-type]
        source_correctness_rate: float | None = correct_count / len(with_doc_id)
    else:
        source_correctness_rate = None

    return {
        "hit_rate_at_k": round(hit_rate, 4),
        "mrr": round(mrr, 4),
        "empty_retrieval_rate": round(empty_retrieval_rate, 4),
        "source_correctness_rate": (
            round(source_correctness_rate, 4) if source_correctness_rate is not None else None
        ),
        "answerable_count": len(answerable),
        "unanswerable_count": len(unanswerable),
        "total": len(results),
    }
