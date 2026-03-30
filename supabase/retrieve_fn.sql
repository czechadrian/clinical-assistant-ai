-- ============================================================
-- Kliniczny Asystent AI — match_chunks retrieval function (Day 18)
-- Run in: Supabase Dashboard → SQL Editor
--
-- Prerequisites:
--   - supabase/chunks_schema.sql must be applied (doc_chunks table exists)
--   - pgvector extension must be enabled (CREATE EXTENSION vector)
--   - At least some doc_chunks rows must have embedding IS NOT NULL
--     (run the ingest worker to populate them)
--
-- Idempotent: CREATE OR REPLACE is safe to re-run.
-- ============================================================


-- ============================================================
-- match_chunks
--
-- Called via PostgREST RPC:
--   POST /rest/v1/rpc/match_chunks
--   {"query_embedding": [0.1, 0.2, ...], "match_count": 5}
--
-- Parameter type: float8[] (double precision array)
--   PostgREST receives a JSON array of numbers and casts it to float8[]
--   using standard Postgres JSON-to-array conversion. This is more
--   reliable than passing a vector(1536) literal directly, which would
--   require pgvector to register a json→vector cast.
--   The cast float8[]::vector(1536) is defined by pgvector.
--
-- Score: 1 - cosine_distance. Range [0, 1]; higher = more similar.
--   pgvector's <=> operator computes cosine distance (not similarity),
--   so we invert: score = 1 - distance.
--
-- SECURITY INVOKER (default): function runs as the calling user.
--   RLS on doc_chunks and docs applies normally. Since both tables allow
--   SELECT for any authenticated role, all authenticated callers can use
--   this function.
--
-- search_path locked to public: prevents search-path injection.
-- ============================================================

create or replace function public.match_chunks(
    query_embedding float8[],
    match_count     int default 5
)
returns table (
    chunk_id    uuid,
    doc_id      uuid,
    title       text,
    section     text,
    content     text,
    score       float8
)
language sql
stable
security invoker
set search_path = public
as $$
    select
        dc.id                                                       as chunk_id,
        dc.doc_id,
        d.title,
        dc.section,
        dc.content,
        1 - (dc.embedding <=> query_embedding::vector(1536))       as score
    from   public.doc_chunks dc
    join   public.docs       d  on d.id = dc.doc_id
    where  dc.embedding is not null
    order  by dc.embedding <=> query_embedding::vector(1536)
    limit  match_count;
$$;

-- Allow any authenticated user to call the function.
-- (The underlying tables already allow authenticated SELECT via RLS.)
grant execute on function public.match_chunks to authenticated;


-- ============================================================
-- Optional: HNSW vector index (add AFTER embeddings are populated)
--
-- An HNSW index dramatically speeds up similarity search for large
-- datasets but requires data to build on. Adding it to an empty column
-- wastes space and provides no benefit.
--
-- Run this once you have > ~100 indexed documents:
--
--   create index concurrently idx_chunks_embedding_hnsw
--       on public.doc_chunks
--       using hnsw (embedding vector_cosine_ops)
--       with (m = 16, ef_construction = 64);
--
-- Parameters to tune with real data:
--   m              = graph connectivity (16 = default; higher = better recall, more RAM)
--   ef_construction = build-time beam width (64 = default; higher = better index quality)
-- ============================================================
