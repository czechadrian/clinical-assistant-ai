# Ingest Pipeline Contract — Kliniczny Asystent AI

This document defines the interface between the document metadata layer (Day 15) and the future embedding/RAG pipeline. It is the source of truth for the ingest worker.

---

## 1. What exists today (Day 15)

| Component | Status |
|---|---|
| `docs` table in Supabase | ✅ Created |
| `medical_docs` Storage bucket | ✅ Created (manual step) |
| Admin upload UI (`/admin/docs`) | ✅ Created |
| `GET /docs` API endpoint | ✅ Created |
| `POST /docs` API endpoint | ✅ Created |
| Chunking / embedding | ❌ Not yet |
| Vector store integration | ❌ Not yet |
| RAG retrieval in `/chat` | ❌ Not yet |

---

## 2. `docs` table schema

```sql
id           uuid        PK, generated
title        text        Human-readable document title
filename     text        Original filename (for display)
storage_path text        UNIQUE — path within the medical_docs bucket
file_hash    text        SHA-256 hex of raw file bytes (64 chars)
version      text        Manual version label, default '1'
status       text        'pending' | 'indexed' | 'failed'
created_at   timestamptz Auto-set on insert
updated_at   timestamptz Auto-bumped by trigger on update
```

---

## 3. Processing lifecycle

```
Admin uploads file
       │
       ▼
Storage: medical_docs/{timestamp}-{filename}
       │
       ▼
POST /docs → docs row inserted, status = 'pending'
       │
       ▼  (async, future)
Ingest worker polls for status = 'pending'
       │
       ├─ Chunk document (fixed-size or semantic)
       ├─ Generate embeddings (Claude Embeddings / OpenAI)
       ├─ Insert chunks into vector store (pgvector table)
       └─ UPDATE docs SET status = 'indexed'
              or SET status = 'failed' on error
```

---

## 4. Document versioning

- **Version field**: set by the admin uploader, default `'1'`. Increment manually for revised documents.
- **Hash-based idempotency**: the ingest worker must check `file_hash` before re-embedding. If a doc with the same `file_hash` is already `'indexed'`, skip it.
- **Re-index trigger**: an admin can force re-index by setting `status = 'pending'` on an existing row. The ingest worker picks it up on next poll.

**Deduplication query** (ingest worker runs this before processing):
```sql
SELECT id FROM docs WHERE file_hash = $1 AND status = 'indexed' LIMIT 1;
```
If a row is returned, skip and mark the new upload as a duplicate (or DELETE it from `docs`).

---

## 5. Fields needed for chunking/embedding (future)

When the chunking table is added, it will reference `docs.id`:

```sql
-- Future table (NOT yet created)
CREATE TABLE doc_chunks (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    doc_id      uuid NOT NULL REFERENCES docs(id) ON DELETE CASCADE,
    chunk_index int  NOT NULL,
    content     text NOT NULL,          -- raw chunk text (not stored in logs)
    embedding   vector(1536),           -- pgvector extension
    token_count int,
    created_at  timestamptz NOT NULL DEFAULT now()
);
```

The ingest worker will need access to:
- `docs.storage_path` — to download the file from Storage
- `docs.file_hash` — for deduplication
- `docs.version` — to tag chunks for filtering
- `docs.id` — as the foreign key on `doc_chunks`

---

## 6. RAG retrieval contract (Week 3+)

When the `/chat` endpoint integrates RAG:

1. The `classify_input` result (`classified.for_llm()`) is used as the query.
2. The retrieval function returns a ranked list of `doc_chunks.content` + their source `docs.title` and `docs.id`.
3. The `SYSTEM_PROMPT` is augmented with retrieved chunks, wrapped in `<retrieved_context>` tags to isolate them from the instruction space.
4. The assistant response's `sources[]` field references `docs.id` and `docs.title`.
5. **Strict privacy rule**: chunk content is never stored in logs or `_meta` fields.

---

## 7. Security notes

- The `docs` table uses the same Supabase RLS pattern: authenticated read, admin-only write.
- The ingest worker (Python, runs server-side) uses the **service role key** to read files from Storage and update `doc_chunks`. It never forwards this key to the frontend.
- Admin role is set via: `UPDATE profiles SET role = 'admin' WHERE id = '<uuid>';`
- The `file_hash` is SHA-256 of raw bytes — suitable for deduplication, not for security (no signed hashes).
