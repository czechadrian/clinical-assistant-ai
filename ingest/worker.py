"""
worker.py — ingest worker v1.

Downloads documents from Supabase Storage, extracts text, chunks it,
and writes doc_chunks rows to Postgres via PostgREST.

Usage (run from the ingest/ directory):

    # Process one document by ID (always re-processes, even if already indexed):
    uv run python worker.py --doc-id <uuid>

    # Process all documents with status='pending':
    uv run python worker.py --all

Auth model:
    The worker uses the service role key, which bypasses PostgREST RLS entirely.
    This is correct and intentional — the ingest process runs server-side and
    must write doc_chunks rows that regular users cannot write directly.
    Never expose the service role key to the frontend.

Idempotency:
    --all   only fetches docs with status='pending', so indexed docs are skipped
            automatically. If a doc is stuck in 'pending' with old chunks (e.g.
            a previous run failed mid-way), existing chunks are deleted before
            re-inserting.
    --doc-id always re-processes the named doc. Useful to force a re-index after
            the admin updates the file and resets status='pending'.

Supported formats:
    PDF  (.pdf)   — pypdf text extraction (v1)
    Text (.txt)   — UTF-8, fallback to latin-1
    Markdown (.md)— same as .txt
    DOCX          — NOT supported in v1 (label as optional in future)
"""

import argparse
import asyncio
import io
import logging
import time
from typing import Any

import httpx
import pypdf

from chunker import chunk_text
from embedder import OpenAIEmbedder
from settings import Settings, configure_logging

logger = logging.getLogger(__name__)

# Storage bucket name — must match the bucket created in Supabase Dashboard.
_BUCKET = "medical_docs"

# Timeout for HTTP calls. Storage downloads can be large; 60 s is generous
# for typical clinical guideline PDFs (< 20 MB).
_TIMEOUT = httpx.Timeout(connect=10.0, read=60.0, write=10.0, pool=5.0)


# ---------------------------------------------------------------------------
# PostgREST + Storage helpers
# ---------------------------------------------------------------------------


def _service_headers(settings: Settings) -> dict[str, str]:
    """Headers that bypass PostgREST RLS via the service role key.

    apikey    — identifies the project at the API-gateway level.
    Authorization — PostgREST evaluates RLS as the service role (bypasses all policies).
    """
    return {
        "apikey": settings.supabase_service_role_key,
        "Authorization": f"Bearer {settings.supabase_service_role_key}",
        "Content-Type": "application/json",
    }


async def _db_get(
    client: httpx.AsyncClient,
    path: str,
    params: dict[str, str],
    settings: Settings,
) -> list[dict]:
    resp = await client.get(
        f"{settings.supabase_url}/rest/v1{path}",
        params=params,
        headers=_service_headers(settings),
    )
    resp.raise_for_status()
    return resp.json()  # type: ignore[return-value]


async def _db_post(
    client: httpx.AsyncClient,
    path: str,
    payload: Any,
    settings: Settings,
) -> None:
    resp = await client.post(
        f"{settings.supabase_url}/rest/v1{path}",
        json=payload,
        headers={**_service_headers(settings), "Prefer": "return=minimal"},
    )
    resp.raise_for_status()


async def _db_patch(
    client: httpx.AsyncClient,
    path: str,
    params: dict[str, str],
    payload: dict,
    settings: Settings,
) -> None:
    resp = await client.patch(
        f"{settings.supabase_url}/rest/v1{path}",
        params=params,
        json=payload,
        headers={**_service_headers(settings), "Prefer": "return=minimal"},
    )
    resp.raise_for_status()


async def _db_delete(
    client: httpx.AsyncClient,
    path: str,
    params: dict[str, str],
    settings: Settings,
) -> None:
    resp = await client.delete(
        f"{settings.supabase_url}/rest/v1{path}",
        params=params,
        headers=_service_headers(settings),
    )
    resp.raise_for_status()


async def _storage_download(
    client: httpx.AsyncClient,
    storage_path: str,
    settings: Settings,
) -> bytes:
    """Download a file from the private medical_docs Storage bucket."""
    url = f"{settings.supabase_url}/storage/v1/object/{_BUCKET}/{storage_path}"
    resp = await client.get(
        url,
        headers={
            "apikey": settings.supabase_service_role_key,
            "Authorization": f"Bearer {settings.supabase_service_role_key}",
        },
    )
    resp.raise_for_status()
    return resp.content


# ---------------------------------------------------------------------------
# Text extraction
# ---------------------------------------------------------------------------


def extract_text(file_bytes: bytes, filename: str) -> str:
    """Extract plain text from file bytes.

    Supported in v1: PDF, TXT, MD.
    DOCX support is optional and not implemented here.

    Raises ValueError for unsupported types or empty extraction.
    """
    lower = filename.lower()

    if lower.endswith(".pdf"):
        reader = pypdf.PdfReader(io.BytesIO(file_bytes))
        pages: list[str] = []
        for page in reader.pages:
            # extract_text() returns "" for image-only pages (scanned PDFs).
            text = page.extract_text() or ""
            if text.strip():
                pages.append(text)
        return "\n\n".join(pages)

    if lower.endswith((".txt", ".md")):
        # Try UTF-8 first; fall back to latin-1 for legacy documents.
        try:
            return file_bytes.decode("utf-8")
        except UnicodeDecodeError:
            return file_bytes.decode("latin-1")

    raise ValueError(f"Unsupported file type: {filename!r}. Supported: .pdf, .txt, .md")


# ---------------------------------------------------------------------------
# Core processing
# ---------------------------------------------------------------------------


async def process_doc(
    doc: dict,
    client: httpx.AsyncClient,
    embedder: OpenAIEmbedder,
    settings: Settings,
) -> None:
    """Download, extract, chunk, embed, and store one document.

    Updates docs.status to 'indexed' on success or 'failed' on error.
    Always deletes existing chunks for this doc_id before inserting new ones
    (idempotent: safe to call multiple times on the same document).
    """
    doc_id: str = doc["id"]
    filename: str = doc["filename"]
    storage_path: str = doc["storage_path"]
    file_hash: str = doc["file_hash"]
    start = time.perf_counter()

    logger.info("doc_ingest_start", extra={"doc_id": doc_id, "filename": filename})

    try:
        # 1. Download the file from Storage. -----------------------------------
        file_bytes = await _storage_download(client, storage_path, settings)

        # 2. Extract text. -----------------------------------------------------
        text = extract_text(file_bytes, filename)
        if not text.strip():
            raise ValueError(
                "Text extraction produced no content. "
                "The file may be a scanned image PDF or otherwise unreadable."
            )

        # 3. Chunk. ------------------------------------------------------------
        chunks = chunk_text(text)
        if not chunks:
            raise ValueError("Chunker produced no output from non-empty text.")

        # 4. Embed all chunks. -------------------------------------------------
        # Sent in batches of _EMBED_BATCH_SIZE; SDK handles retry internally.
        # Log only the count — never the chunk text or embeddings themselves.
        logger.info(
            "doc_embedding_start",
            extra={"doc_id": doc_id, "chunk_count": len(chunks)},
        )
        embeddings = await embedder.embed(chunks)
        if len(embeddings) != len(chunks):
            raise ValueError(
                f"Embedder returned {len(embeddings)} vectors for {len(chunks)} chunks."
            )

        # 5. Delete any stale chunks before inserting (clean-slate idempotency).
        # This is a no-op for new documents and a cleanup for re-indexed ones.
        await _db_delete(client, "/doc_chunks", {"doc_id": f"eq.{doc_id}"}, settings)

        # 6. Build and insert chunk rows with embeddings. ----------------------
        # token_count is a rough estimate (4 chars ≈ 1 token for Latin script).
        # embedding is a list[float]; PostgREST receives it as a JSON array and
        # Postgres casts it to float8[], then the SQL function casts to vector(1536).
        rows = [
            {
                "doc_id": doc_id,
                "chunk_index": i,
                "content": chunk,
                "token_count": len(chunk) // 4,
                "embedding": embedding,
            }
            for i, (chunk, embedding) in enumerate(zip(chunks, embeddings, strict=True))
        ]
        await _db_post(client, "/doc_chunks", rows, settings)

        # 6. Mark the parent doc as indexed. -----------------------------------
        await _db_patch(
            client,
            "/docs",
            {"id": f"eq.{doc_id}"},
            {"status": "indexed"},
            settings,
        )

        elapsed_ms = round((time.perf_counter() - start) * 1000)
        logger.info(
            "doc_ingest_done",
            extra={
                "doc_id": doc_id,
                "filename": filename,
                "file_hash": file_hash,
                "chunk_count": len(chunks),
                "elapsed_ms": elapsed_ms,
                "status": "indexed",
            },
        )

    except Exception as exc:
        elapsed_ms = round((time.perf_counter() - start) * 1000)
        logger.error(
            "doc_ingest_failed",
            extra={
                "doc_id": doc_id,
                "filename": filename,
                "elapsed_ms": elapsed_ms,
                # str(exc) is safe: it never contains raw document content.
                "error": str(exc),
            },
        )
        # Best-effort: mark as failed so the admin knows to investigate.
        # If this patch also fails, the doc stays 'pending' and will be retried.
        try:
            await _db_patch(
                client,
                "/docs",
                {"id": f"eq.{doc_id}"},
                {"status": "failed"},
                settings,
            )
        except Exception:
            logger.error("doc_status_update_failed", extra={"doc_id": doc_id})
        raise


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------

_DOC_SELECT = "id,filename,storage_path,file_hash,status,version"


async def run_one(doc_id: str, settings: Settings) -> None:
    """Process a single document by ID regardless of its current status."""
    embedder = OpenAIEmbedder(settings.openai_api_key, settings.embed_model)
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            rows = await _db_get(
                client,
                "/docs",
                {"id": f"eq.{doc_id}", "select": _DOC_SELECT},
                settings,
            )
            if not rows:
                raise SystemExit(f"Document not found: {doc_id}")
            await process_doc(rows[0], client, embedder, settings)
    finally:
        await embedder.aclose()


async def run_all(settings: Settings) -> None:
    """Process all documents with status='pending', oldest first."""
    embedder = OpenAIEmbedder(settings.openai_api_key, settings.embed_model)
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            rows = await _db_get(
                client,
                "/docs",
                {
                    "status": "eq.pending",
                    "select": _DOC_SELECT,
                    "order": "created_at.asc",
                },
                settings,
            )
            if not rows:
                logger.info("no_pending_docs")
                return

            logger.info("run_all_start", extra={"doc_count": len(rows)})
            failed = 0
            for doc in rows:
                try:
                    await process_doc(doc, client, embedder, settings)
                except Exception:
                    # Continue processing remaining docs even if one fails.
                    failed += 1

            logger.info(
                "run_all_done",
                extra={
                    "doc_count": len(rows),
                    "failed": failed,
                    "succeeded": len(rows) - failed,
                },
            )
    finally:
        await embedder.aclose()


def main() -> None:
    from dotenv import load_dotenv

    load_dotenv()

    # Configure logging before parsing args so any startup errors are structured.
    import os

    configure_logging(os.getenv("APP_ENV", "local"))
    settings = Settings.from_env()

    if not settings.openai_api_key:
        raise SystemExit(
            "OPENAI_API_KEY is not set in the environment. "
            "Embeddings cannot be generated without it."
        )

    parser = argparse.ArgumentParser(
        description="Ingest worker v1 — chunks docs and writes to doc_chunks table."
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--doc-id",
        metavar="UUID",
        help="Process a specific document by ID (re-processes even if already indexed).",
    )
    group.add_argument(
        "--all",
        action="store_true",
        help="Process all documents with status='pending'.",
    )
    args = parser.parse_args()

    if args.all:
        asyncio.run(run_all(settings))
    else:
        asyncio.run(run_one(args.doc_id, settings))


if __name__ == "__main__":
    main()
