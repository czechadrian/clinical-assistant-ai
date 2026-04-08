"""
Tests for ingest/eval/metrics.py — pure metric computation.

All tests are deterministic and require no I/O or network calls.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from eval.metrics import (
    RetrievalResult,
    compute_metrics,
    doc_id_hit,
    first_hit_rank,
    is_hit,
    keyword_hit,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _result(
    question_id: str,
    answerable: bool,
    retrieved: list[dict],
    expected_doc_id: str | None = None,
    expected_doc_keywords: list[str] | None = None,
) -> RetrievalResult:
    return RetrievalResult(
        question_id=question_id,
        answerable=answerable,
        expected_doc_id=expected_doc_id,
        expected_doc_keywords=expected_doc_keywords or [],
        retrieved=retrieved,
    )


# ---------------------------------------------------------------------------
# keyword_hit
# ---------------------------------------------------------------------------


def test_keyword_hit_true():
    chunks = [{"title": "Wytyczne OZW PTK 2023", "doc_id": "abc"}]
    assert keyword_hit(chunks, ["OZW", "zawał"]) is True


def test_keyword_hit_case_insensitive():
    chunks = [{"title": "wytyczne ozw ptk", "doc_id": "abc"}]
    assert keyword_hit(chunks, ["OZW"]) is True


def test_keyword_hit_no_match():
    chunks = [{"title": "Zapalenie płuc", "doc_id": "abc"}]
    assert keyword_hit(chunks, ["OZW", "zawał"]) is False


def test_keyword_hit_empty_retrieved():
    assert keyword_hit([], ["OZW"]) is False


def test_keyword_hit_empty_keywords():
    chunks = [{"title": "Anything", "doc_id": "abc"}]
    assert keyword_hit(chunks, []) is False


def test_keyword_hit_partial_substring():
    # "kardiolog" is a substring of "kardiologia"
    chunks = [{"title": "Wytyczne kardiologia 2023", "doc_id": "x"}]
    assert keyword_hit(chunks, ["kardiolog"]) is True


# ---------------------------------------------------------------------------
# doc_id_hit
# ---------------------------------------------------------------------------


def test_doc_id_hit_first_chunk():
    chunks = [{"doc_id": "doc-aaa"}, {"doc_id": "doc-bbb"}]
    assert doc_id_hit(chunks, "doc-aaa") is True


def test_doc_id_hit_second_chunk():
    chunks = [{"doc_id": "doc-aaa"}, {"doc_id": "doc-bbb"}]
    assert doc_id_hit(chunks, "doc-bbb") is True


def test_doc_id_hit_not_found():
    chunks = [{"doc_id": "doc-aaa"}]
    assert doc_id_hit(chunks, "doc-zzz") is False


def test_doc_id_hit_empty():
    assert doc_id_hit([], "doc-aaa") is False


# ---------------------------------------------------------------------------
# first_hit_rank
# ---------------------------------------------------------------------------


def test_first_hit_rank_by_doc_id_position_2():
    chunks = [
        {"doc_id": "x", "title": "Unrelated"},
        {"doc_id": "target", "title": "Unrelated"},
    ]
    assert first_hit_rank(chunks, "target", []) == 2


def test_first_hit_rank_by_keyword_position_2():
    chunks = [
        {"doc_id": "a", "title": "Zapalenie płuc"},
        {"doc_id": "b", "title": "Wytyczne OZW"},
    ]
    assert first_hit_rank(chunks, None, ["OZW"]) == 2


def test_first_hit_rank_no_hit():
    chunks = [{"doc_id": "a", "title": "Unrelated"}]
    assert first_hit_rank(chunks, "target", ["missing"]) == 0


def test_first_hit_rank_position_1():
    chunks = [{"doc_id": "target", "title": "OZW guidelines"}]
    assert first_hit_rank(chunks, "target", []) == 1


def test_first_hit_rank_doc_id_preferred_over_keyword():
    # Both doc_id and keyword would match position 1; doc_id wins naturally
    chunks = [{"doc_id": "target", "title": "OZW guidelines"}]
    assert first_hit_rank(chunks, "target", ["OZW"]) == 1


def test_first_hit_rank_empty_retrieved():
    assert first_hit_rank([], "target", ["OZW"]) == 0


# ---------------------------------------------------------------------------
# is_hit
# ---------------------------------------------------------------------------


def test_is_hit_by_keyword():
    r = _result("q1", True, [{"doc_id": "a", "title": "OZW PTK"}], expected_doc_keywords=["OZW"])
    assert is_hit(r) is True


def test_is_hit_by_doc_id():
    r = _result("q1", True, [{"doc_id": "target", "title": "X"}], expected_doc_id="target")
    assert is_hit(r) is True


def test_is_hit_miss():
    r = _result(
        "q1", True, [{"doc_id": "other", "title": "Unrelated"}], expected_doc_keywords=["OZW"]
    )
    assert is_hit(r) is False


# ---------------------------------------------------------------------------
# compute_metrics — edge cases
# ---------------------------------------------------------------------------


def test_compute_metrics_empty():
    m = compute_metrics([])
    assert m["hit_rate_at_k"] == 0.0
    assert m["mrr"] == 0.0
    assert m["empty_retrieval_rate"] == 0.0
    assert m["source_correctness_rate"] is None
    assert m["total"] == 0


def test_compute_metrics_all_answerable_hits():
    results = [
        _result(
            "q1", True, [{"doc_id": "a", "title": "OZW guidelines"}], expected_doc_keywords=["OZW"]
        ),
        _result(
            "q2",
            True,
            [{"doc_id": "b", "title": "POChP guidelines"}],
            expected_doc_keywords=["POChP"],
        ),
    ]
    m = compute_metrics(results)
    assert m["hit_rate_at_k"] == 1.0
    assert m["mrr"] == 1.0
    assert m["empty_retrieval_rate"] == 0.0
    assert m["answerable_count"] == 2
    assert m["unanswerable_count"] == 0


def test_compute_metrics_no_hits():
    results = [
        _result("q1", True, [{"doc_id": "a", "title": "Unrelated"}], expected_doc_keywords=["OZW"]),
    ]
    m = compute_metrics(results)
    assert m["hit_rate_at_k"] == 0.0
    assert m["mrr"] == 0.0


def test_compute_metrics_empty_retrieval_unanswerable():
    results = [
        _result("q1", False, []),
        _result("q2", False, [{"doc_id": "x", "title": "Found something"}]),
    ]
    m = compute_metrics(results)
    assert m["empty_retrieval_rate"] == 0.5
    assert m["unanswerable_count"] == 2


def test_compute_metrics_mrr_second_hit():
    # Match is at rank 2 → RR = 0.5
    results = [
        _result(
            "q1",
            True,
            [
                {"doc_id": "wrong", "title": "Unrelated"},
                {"doc_id": "target", "title": "OZW"},
            ],
            expected_doc_id="target",
        ),
    ]
    m = compute_metrics(results)
    assert m["mrr"] == pytest.approx(0.5)
    assert m["hit_rate_at_k"] == 1.0


def test_compute_metrics_mrr_averaged():
    # q1: hit at rank 1 (RR=1.0), q2: hit at rank 2 (RR=0.5) → MRR=0.75
    results = [
        _result("q1", True, [{"doc_id": "t", "title": "OZW"}], expected_doc_id="t"),
        _result(
            "q2",
            True,
            [{"doc_id": "wrong", "title": "X"}, {"doc_id": "t2", "title": "POChP"}],
            expected_doc_keywords=["POChP"],
        ),
    ]
    m = compute_metrics(results)
    assert m["mrr"] == pytest.approx(0.75)


def test_compute_metrics_source_correctness_rate():
    results = [
        _result("q1", True, [{"doc_id": "target", "title": "X"}], expected_doc_id="target"),
        _result("q2", True, [{"doc_id": "wrong", "title": "X"}], expected_doc_id="target"),
    ]
    m = compute_metrics(results)
    assert m["source_correctness_rate"] == 0.5


def test_compute_metrics_source_correctness_none_when_no_doc_ids():
    results = [
        _result("q1", True, [{"doc_id": "a", "title": "OZW"}], expected_doc_keywords=["OZW"]),
    ]
    m = compute_metrics(results)
    assert m["source_correctness_rate"] is None


def test_compute_metrics_mixed():
    results = [
        _result("q1", True, [{"doc_id": "a", "title": "OZW PTK"}], expected_doc_keywords=["OZW"]),
        _result("q2", True, [{"doc_id": "b", "title": "Unrelated"}], expected_doc_keywords=["OZW"]),
        _result("q3", False, []),
        _result("q4", False, [{"doc_id": "c", "title": "Something"}]),
    ]
    m = compute_metrics(results)
    assert m["hit_rate_at_k"] == 0.5
    assert m["answerable_count"] == 2
    assert m["unanswerable_count"] == 2
    assert m["empty_retrieval_rate"] == 0.5
    assert m["total"] == 4


def test_compute_metrics_all_unanswerable_empty():
    results = [
        _result("q1", False, []),
        _result("q2", False, []),
        _result("q3", False, []),
    ]
    m = compute_metrics(results)
    assert m["empty_retrieval_rate"] == 1.0
    assert m["hit_rate_at_k"] == 0.0
    assert m["mrr"] == 0.0


# ---------------------------------------------------------------------------
# Smoke: dry_run mode via run_eval (no I/O, no API calls)
# ---------------------------------------------------------------------------


def test_run_eval_dry_run_produces_zero_metrics():
    """run_eval --dry-run should complete and produce 0.0 for retrieval metrics."""
    import asyncio
    import os

    os.environ.setdefault("SUPABASE_URL", "http://localhost:54321")
    os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "test-key")
    os.environ.setdefault("OPENAI_API_KEY", "")

    from scripts.run_eval import run_eval

    # Run only 2 questions in dry-run mode to keep the test fast.
    asyncio.run(
        run_eval(
            top_k=5,
            question_ids=["q001", "q021"],
            dry_run=True,
        )
    )

    # Verify a result file was written and is valid JSON.
    from pathlib import Path

    results_dir = Path(__file__).resolve().parent.parent / "eval" / "results"
    json_files = sorted(results_dir.glob("*.json"))
    assert json_files, "No result files written by dry run"
    report = __import__("json").loads(json_files[-1].read_text())
    assert report["dry_run"] is True
    assert report["metrics"]["hit_rate_at_k"] == 0.0
    assert report["metrics"]["empty_retrieval_rate"] == 1.0  # q021 unanswerable, empty
