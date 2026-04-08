#!/usr/bin/env python3
"""
run_eval.py — offline RAG evaluation runner for Kliniczny Asystent AI.

Calls the embedding API + Supabase PostgREST RPC directly (service role key,
same as worker.py).  Does NOT import main.py — no FastAPI side effects.

Usage (run from repo root):
    uv run --project ingest python ingest/scripts/run_eval.py
    uv run --project ingest python ingest/scripts/run_eval.py --top-k 10
    uv run --project ingest python ingest/scripts/run_eval.py --dry-run
    uv run --project ingest python ingest/scripts/run_eval.py --question-id q001 --question-id q002

Options:
    --top-k N          Number of chunks to retrieve per question (default: 5)
    --dry-run          Skip embedding + retrieval; all retrieved=[] (smoke test / CI)
    --question-id ID   Run only the specified question(s); repeatable

Outputs (written to ingest/eval/results/):
    <timestamp>.json   Full report with per-question details
    <timestamp>.md     Human-readable Markdown summary

Privacy: question text and chunk content are never written to stdout or logs.
Only counts, IDs, and metric values are logged.
"""

import argparse
import asyncio
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

import httpx
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Path bootstrap — ingest/ must be on sys.path before project imports
# ---------------------------------------------------------------------------
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_INGEST_DIR = _REPO_ROOT / "ingest"
sys.path.insert(0, str(_INGEST_DIR))

from embedder import OpenAIEmbedder  # noqa: E402
from eval.metrics import RetrievalResult, compute_metrics, first_hit_rank, is_hit  # noqa: E402
from settings import Settings, configure_logging  # noqa: E402

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DATASET_PATH = _INGEST_DIR / "eval" / "dataset.json"
_RESULTS_DIR = _INGEST_DIR / "eval" / "results"
_TIMEOUT = httpx.Timeout(connect=10.0, read=30.0, write=10.0, pool=5.0)


# ---------------------------------------------------------------------------
# PostgREST helpers (service role — same pattern as worker.py)
# ---------------------------------------------------------------------------


def _service_headers(settings: Settings) -> dict[str, str]:
    return {
        "apikey": settings.supabase_service_role_key,
        "Authorization": f"Bearer {settings.supabase_service_role_key}",
        "Content-Type": "application/json",
    }


async def _match_chunks(
    client: httpx.AsyncClient,
    embedding: list[float],
    top_k: int,
    settings: Settings,
) -> list[dict]:
    """Call the match_chunks Postgres function via PostgREST RPC."""
    resp = await client.post(
        f"{settings.supabase_url}/rest/v1/rpc/match_chunks",
        json={"query_embedding": embedding, "match_count": top_k},
        headers=_service_headers(settings),
    )
    resp.raise_for_status()
    return resp.json()  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Report rendering
# ---------------------------------------------------------------------------


def _render_markdown(report: dict) -> str:
    m = report["metrics"]
    ts = report["timestamp"]
    top_k = report["top_k"]
    lines = [
        "# RAG Evaluation Report",
        "",
        f"**Date:** {ts}  ",
        f"**top_k:** {top_k}  ",
        f"**Questions:** {m['total']} "
        f"({m['answerable_count']} answerable, {m['unanswerable_count']} unanswerable)  ",
        f"**Dry run:** {report['dry_run']}",
        "",
        "## Aggregate Metrics",
        "",
        "| Metric | Value |",
        "|--------|-------|",
        f"| hit_rate@{top_k} | {m['hit_rate_at_k']:.3f} |",
        f"| mrr | {m['mrr']:.3f} |",
        f"| empty_retrieval_rate | {m['empty_retrieval_rate']:.3f} |",
    ]
    if m["source_correctness_rate"] is not None:
        lines.append(f"| source_correctness_rate | {m['source_correctness_rate']:.3f} |")

    lines += [
        "",
        "## Per-Question Results",
        "",
        "| ID | Category | Answerable | Retrieved | Hit | Rank |",
        "|----|----------|-----------|-----------|-----|------|",
    ]
    for pq in report["per_question"]:
        hit_val = pq["hit"]
        hit_cell = "✓" if hit_val is True else ("✗" if hit_val is False else "N/A")
        rank_cell = str(pq["rank"]) if pq["rank"] is not None else "N/A"
        lines.append(
            f"| {pq['id']} | {pq['category']} | {pq['answerable']} "
            f"| {pq['retrieved_count']} | {hit_cell} | {rank_cell} |"
        )
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Core runner
# ---------------------------------------------------------------------------


async def run_eval(
    top_k: int = 5,
    question_ids: list[str] | None = None,
    dry_run: bool = False,
) -> None:
    load_dotenv()
    configure_logging(os.getenv("APP_ENV", "local"))

    settings = Settings.from_env()

    if not settings.openai_api_key and not dry_run:
        raise SystemExit(
            "OPENAI_API_KEY is not set. "
            "Embeddings cannot be generated without it. "
            "Use --dry-run to skip embeddings."
        )

    dataset = json.loads(_DATASET_PATH.read_text())
    questions: list[dict] = dataset["questions"]
    if question_ids:
        questions = [q for q in questions if q["id"] in set(question_ids)]
    if not questions:
        raise SystemExit("No questions matched the provided --question-id filter(s).")

    _RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(UTC).strftime("%Y-%m-%dT%H-%M-%SZ")

    results: list[RetrievalResult] = []
    per_question: list[dict] = []

    embedder = (
        OpenAIEmbedder(settings.openai_api_key, settings.embed_model) if not dry_run else None
    )

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            for q in questions:
                if dry_run:
                    retrieved: list[dict] = []
                else:
                    assert embedder is not None
                    embeddings = await embedder.embed([q["question"]])
                    retrieved = await _match_chunks(client, embeddings[0], top_k, settings)

                result: RetrievalResult = {
                    "question_id": q["id"],
                    "answerable": q["answerable"],
                    "expected_doc_id": q.get("expected_doc_id"),
                    "expected_doc_keywords": q.get("expected_doc_keywords", []),
                    "retrieved": retrieved,
                }
                results.append(result)

                # Per-question summary — IDs and counts only, no text content.
                hit = is_hit(result) if q["answerable"] else None
                rank = (
                    first_hit_rank(
                        retrieved,
                        q.get("expected_doc_id"),
                        q.get("expected_doc_keywords", []),
                    )
                    if q["answerable"]
                    else None
                )
                per_question.append(
                    {
                        "id": q["id"],
                        "category": q["category"],
                        "answerable": q["answerable"],
                        "retrieved_count": len(retrieved),
                        "hit": hit,
                        "rank": rank,
                    }
                )
    finally:
        if embedder is not None:
            await embedder.aclose()

    metrics = compute_metrics(results)
    report = {
        "version": dataset.get("version", "1.0"),
        "timestamp": ts,
        "top_k": top_k,
        "dry_run": dry_run,
        "metrics": metrics,
        "per_question": per_question,
    }

    json_path = _RESULTS_DIR / f"{ts}.json"
    md_path = _RESULTS_DIR / f"{ts}.md"
    json_path.write_text(json.dumps(report, indent=2, ensure_ascii=False))
    md_path.write_text(_render_markdown(report))

    # Summary to stdout — counts and metrics only, no question text or content.
    print(f"\nEval complete — {len(questions)} questions, top_k={top_k}")
    print(f"  hit_rate@{top_k}:           {metrics['hit_rate_at_k']:.3f}")
    print(f"  mrr:                    {metrics['mrr']:.3f}")
    print(f"  empty_retrieval_rate:   {metrics['empty_retrieval_rate']:.3f}")
    scr = metrics["source_correctness_rate"]
    if scr is not None:
        print(f"  source_correctness:     {scr:.3f}")
    print("\nResults saved to:")
    print(f"  {json_path}")
    print(f"  {md_path}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Offline RAG evaluation runner for Kliniczny Asystent AI."
    )
    parser.add_argument(
        "--top-k",
        type=int,
        default=5,
        help="Number of chunks to retrieve per question (default: 5).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Skip embeddings and retrieval; all retrieved=[] (smoke test / CI).",
    )
    parser.add_argument(
        "--question-id",
        action="append",
        dest="question_ids",
        metavar="ID",
        help="Run only the specified question(s). Repeatable: --question-id q001 --question-id q002",
    )
    args = parser.parse_args()

    asyncio.run(
        run_eval(
            top_k=args.top_k,
            question_ids=args.question_ids,
            dry_run=args.dry_run,
        )
    )


if __name__ == "__main__":
    main()
