-- ============================================================
-- Kliniczny Asystent AI — doc_chunks schema (Day 16)
-- Run in: Supabase Dashboard → SQL Editor
--
-- Prerequisites:
--   1. supabase/docs_schema.sql must already be applied
--      (doc_chunks.doc_id references public.docs)
--
-- Access model: same as docs table —
--   - Any authenticated user can read (global shared knowledge base)
--   - Admin write via PostgREST (rare; for manual corrections)
--   - Ingest worker uses the service role key → bypasses RLS entirely
--     No INSERT policy is needed for regular authenticated users.
--
-- Embedding dimension: vector(1536)
--   Matches OpenAI text-embedding-3-small and text-embedding-ada-002.
--   The embedding column is NULL until the ingest worker populates it.
--
-- Vector similarity index (HNSW) is NOT created here.
--   IVFFlat/HNSW require data to train on and are pointless on an empty
--   table. Add in Day 18 once embeddings are populated:
--     CREATE INDEX idx_chunks_embedding_hnsw ON public.doc_chunks
--     USING hnsw (embedding vector_cosine_ops);
--
-- Idempotent: safe to re-run.
-- ============================================================


-- ============================================================
-- A. Enable pgvector extension
-- ============================================================

create extension if not exists vector
    schema extensions;   -- Supabase convention: extensions live in the
                         -- "extensions" schema, not public.


-- ============================================================
-- B. doc_chunks table
-- ============================================================

create table if not exists public.doc_chunks (
    id          uuid        primary key default gen_random_uuid(),
    doc_id      uuid        not null references public.docs(id) on delete cascade,
    chunk_index int         not null,           -- 0-based position within the document
    content     text        not null,           -- raw chunk text (never stored in logs)
    section     text,                           -- chapter / heading label, if extractable
    page        int,                            -- source page number, if available
    token_count int,                            -- approximate tokens (for context-window planning)
    embedding   vector(1536),                   -- NULL until ingest worker populates

    created_at  timestamptz not null default now(),

    -- Prevents duplicate chunks during ingest retries.
    -- The ingest worker should upsert on this key.
    unique (doc_id, chunk_index)
);


-- ============================================================
-- C. Row Level Security
-- ============================================================

alter table public.doc_chunks enable row level security;

drop policy if exists "chunks: authenticated read"  on public.doc_chunks;
drop policy if exists "chunks: admin insert"        on public.doc_chunks;
drop policy if exists "chunks: admin update"        on public.doc_chunks;
drop policy if exists "chunks: admin delete"        on public.doc_chunks;

-- Any authenticated user (doctor or admin) can read chunks.
create policy "chunks: authenticated read"
    on public.doc_chunks for select
    to authenticated
    using (true);

-- Admin-only direct write via PostgREST (for manual corrections).
-- Normal chunk inserts come from the ingest worker via service role
-- and bypass RLS entirely — this policy is a safety net only.
create policy "chunks: admin insert"
    on public.doc_chunks for insert
    to authenticated
    with check (
        exists (
            select 1 from public.profiles
            where id = auth.uid()
              and role = 'admin'
        )
    );

create policy "chunks: admin update"
    on public.doc_chunks for update
    to authenticated
    using (
        exists (
            select 1 from public.profiles
            where id = auth.uid()
              and role = 'admin'
        )
    )
    with check (
        exists (
            select 1 from public.profiles
            where id = auth.uid()
              and role = 'admin'
        )
    );

create policy "chunks: admin delete"
    on public.doc_chunks for delete
    to authenticated
    using (
        exists (
            select 1 from public.profiles
            where id = auth.uid()
              and role = 'admin'
        )
    );


-- ============================================================
-- D. Indexes
-- ============================================================

-- Filter all chunks for a given document (used by ingest worker and admin tools).
create index if not exists idx_chunks_doc_id
    on public.doc_chunks (doc_id);

-- Ordered retrieval of all chunks within a document (chunk_index scan).
-- Covers both WHERE doc_id = $1 and ORDER BY chunk_index.
create index if not exists idx_chunks_doc_chunk
    on public.doc_chunks (doc_id, chunk_index);

-- NOTE: Vector similarity index is intentionally omitted here.
-- Add after Day 18 when embeddings are populated:
--
--   create index idx_chunks_embedding_hnsw
--       on public.doc_chunks
--       using hnsw (embedding vector_cosine_ops)
--       with (m = 16, ef_construction = 64);
--
-- HNSW parameters (tune after you have real data):
--   m              = max connections per node (default 16, higher = better recall, more RAM)
--   ef_construction = build-time beam width  (default 64, higher = better index, slower build)
