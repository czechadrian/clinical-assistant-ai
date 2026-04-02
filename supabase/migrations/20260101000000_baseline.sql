-- ============================================================
-- Baseline migration — all schema applied before Day 20
-- Sources: schema.sql + docs_schema.sql + chunks_schema.sql + retrieve_fn.sql
--
-- This migration is ALREADY APPLIED to the production database.
-- Mark as applied without running: pnpm db:repair 20260101000000
-- ============================================================

-- ============================================================
-- Kliniczny Asystent AI — Supabase schema
-- Run in: Supabase Dashboard → SQL Editor
--
-- Idempotent: safe to re-run on an existing database.
-- Sections:
--   A. Tables          (CREATE TABLE IF NOT EXISTS)
--   B. Adjust columns  (ALTER TABLE — skip on fresh install)
--   C. Row Level Security
--   D. Indexes
--   E. Trigger         (OPTIONAL — auto-updates conversations.updated_at)
-- ============================================================


-- ============================================================
-- A. Tables
-- ============================================================

create table if not exists public.profiles (
  id         uuid        primary key references auth.users(id) on delete cascade,
  email      text,
  role       text        not null default 'doctor',
  created_at timestamptz not null default now()
);

create table if not exists public.conversations (
  id         uuid        primary key default gen_random_uuid(),
  user_id    uuid        not null references auth.users(id) on delete cascade,
  title      text,                          -- null until set after first message
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table if not exists public.messages (
  id              uuid        primary key default gen_random_uuid(),
  conversation_id uuid        not null references public.conversations(id) on delete cascade,
  user_id         uuid        not null references auth.users(id) on delete cascade,
  role            text        not null check (role in ('user', 'assistant')),
  content         jsonb       not null,     -- AssistantPayload for assistant; plain {text} for user
  created_at      timestamptz not null default now()
);


-- ============================================================
-- B. Adjust existing tables
--    Skip this entire section on a fresh install.
--    Run only the lines that match your current schema gap.
-- ============================================================

-- profiles — add missing columns
alter table public.profiles
  add column if not exists role       text        not null default 'doctor',
  add column if not exists created_at timestamptz not null default now();

-- conversations — add missing columns
alter table public.conversations
  add column if not exists title      text,
  add column if not exists updated_at timestamptz not null default now();

-- messages — migrate content from TEXT to JSONB
-- Only executes the ALTER if content is still stored as text.
-- Requires all existing content values to be valid JSON.
do $$
begin
  if exists (
    select 1 from information_schema.columns
    where table_schema = 'public'
      and table_name   = 'messages'
      and column_name  = 'content'
      and data_type    = 'text'
  ) then
    alter table public.messages
      alter column content type jsonb using content::jsonb;
  end if;
end $$;


-- ============================================================
-- C. Row Level Security
--
-- Drop-then-recreate makes this section idempotent.
--
-- Key rules enforced here that application code cannot override:
--   1. A row is only visible to its owner (auth.uid() check).
--   2. On message INSERT, the target conversation must also
--      belong to auth.uid() — prevents writing into another
--      user's conversation even if the conversation_id is known.
--   3. user_id on INSERT is always forced to auth.uid() via
--      WITH CHECK, so clients cannot spoof a different user_id.
-- ============================================================

alter table public.profiles      enable row level security;
alter table public.conversations  enable row level security;
alter table public.messages       enable row level security;

-- ---- profiles ----
drop policy if exists "profiles: select own"  on public.profiles;
drop policy if exists "profiles: insert own"  on public.profiles;
drop policy if exists "profiles: update own"  on public.profiles;

-- No DELETE policy: profile deletion is handled by auth.users cascade.
create policy "profiles: select own"
  on public.profiles for select
  using (auth.uid() = id);

create policy "profiles: insert own"
  on public.profiles for insert
  with check (auth.uid() = id);

create policy "profiles: update own"
  on public.profiles for update
  using     (auth.uid() = id)
  with check (auth.uid() = id);

-- ---- conversations ----
drop policy if exists "conversations: select own"  on public.conversations;
drop policy if exists "conversations: insert own"  on public.conversations;
drop policy if exists "conversations: update own"  on public.conversations;
drop policy if exists "conversations: delete own"  on public.conversations;

create policy "conversations: select own"
  on public.conversations for select
  using (auth.uid() = user_id);

create policy "conversations: insert own"
  on public.conversations for insert
  with check (auth.uid() = user_id);

create policy "conversations: update own"
  on public.conversations for update
  using     (auth.uid() = user_id)
  with check (auth.uid() = user_id);

create policy "conversations: delete own"
  on public.conversations for delete
  using (auth.uid() = user_id);

-- ---- messages ----
drop policy if exists "messages: select own"  on public.messages;
drop policy if exists "messages: insert own"  on public.messages;
drop policy if exists "messages: update own"  on public.messages;
drop policy if exists "messages: delete own"  on public.messages;

create policy "messages: select own"
  on public.messages for select
  using (auth.uid() = user_id);

-- Two conditions on INSERT:
--   a) user_id in the new row must equal auth.uid()
--   b) the target conversation must belong to auth.uid()
create policy "messages: insert own"
  on public.messages for insert
  with check (
    auth.uid() = user_id
    and exists (
      select 1 from public.conversations c
      where  c.id      = conversation_id
        and  c.user_id = auth.uid()
    )
  );

create policy "messages: update own"
  on public.messages for update
  using     (auth.uid() = user_id)
  with check (auth.uid() = user_id);

create policy "messages: delete own"
  on public.messages for delete
  using (auth.uid() = user_id);


-- ============================================================
-- D. Indexes
-- ============================================================

-- Sidebar query: all conversations for a user, newest first.
-- Covers both the WHERE (user_id) and the ORDER BY (updated_at).
create index if not exists idx_conversations_user_updated
  on public.conversations (user_id, updated_at desc);

-- Message thread query: all messages in a conversation in order.
-- Covers both the WHERE (conversation_id) and the ORDER BY (created_at).
create index if not exists idx_messages_conversation_created
  on public.messages (conversation_id, created_at asc);

-- Used by RLS policies and the trigger's UPDATE.
create index if not exists idx_messages_user_id
  on public.messages (user_id);


-- ============================================================
-- E. Trigger — auto-update conversations.updated_at  (OPTIONAL)
--
-- Fires after every INSERT on messages and bumps updated_at on
-- the parent conversation. This keeps the sidebar sorted by
-- activity without any application-level bookkeeping.
--
-- security definer + set search_path: the function runs as its
-- owner (postgres) so it can UPDATE conversations even when
-- called from a restricted role. The explicit search_path
-- prevents search-path injection attacks.
-- ============================================================

create or replace function public.fn_touch_conversation()
  returns trigger
  language plpgsql
  security definer
  set search_path = public
as $$
begin
  update public.conversations
  set    updated_at = now()
  where  id = new.conversation_id;
  return new;
end;
$$;

drop trigger if exists trg_messages_touch_conversation on public.messages;

create trigger trg_messages_touch_conversation
  after insert on public.messages
  for each row
  execute function public.fn_touch_conversation();

-- ============================================================
-- Kliniczny Asystent AI — Document metadata schema (Day 15)
-- Run in: Supabase Dashboard → SQL Editor
--
-- Design choice: Option A — Global shared docs, read-only for
-- all doctors, write-restricted to admin role.
--
-- Rationale: medical guidelines are institutional knowledge
-- shared across all clinicians. Per-user docs would create
-- silos and maintenance overhead. Admin-controlled ensures
-- quality and compliance.
--
-- Admin check: uses profiles.role = 'admin' (already in schema).
-- Set role via: UPDATE profiles SET role = 'admin' WHERE id = '<uuid>';
--
-- Storage bucket: medical_docs
--   Create manually in Supabase Dashboard → Storage → New bucket
--   Settings: private (not public), no file size limit set here.
--   Then run the storage RLS section below.
--
-- Idempotent: safe to re-run.
-- ============================================================


-- ============================================================
-- A. docs table
-- ============================================================

create table if not exists public.docs (
    id           uuid        primary key default gen_random_uuid(),
    title        text        not null,
    filename     text        not null,
    storage_path text        not null unique,  -- path in Supabase Storage bucket
    file_hash    text        not null,         -- SHA-256 hex of file content
    version      text        not null default '1',
    -- pending: uploaded, not yet processed by ingest worker
    -- indexed:  chunks + embeddings exist in the vector store
    -- failed:   ingest worker encountered an unrecoverable error
    status       text        not null default 'pending'
                             check (status in ('pending', 'indexed', 'failed')),
    created_at   timestamptz not null default now(),
    updated_at   timestamptz not null default now()
);


-- ============================================================
-- B. Row Level Security
-- ============================================================

alter table public.docs enable row level security;

drop policy if exists "docs: authenticated read"  on public.docs;
drop policy if exists "docs: admin insert"        on public.docs;
drop policy if exists "docs: admin update"        on public.docs;
drop policy if exists "docs: admin delete"        on public.docs;

-- Any authenticated user (doctor role or admin) can read
create policy "docs: authenticated read"
    on public.docs for select
    to authenticated
    using (true);

-- Only admins can insert (admin check via profiles.role)
create policy "docs: admin insert"
    on public.docs for insert
    to authenticated
    with check (
        exists (
            select 1 from public.profiles
            where id = auth.uid()
              and role = 'admin'
        )
    );

create policy "docs: admin update"
    on public.docs for update
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

create policy "docs: admin delete"
    on public.docs for delete
    to authenticated
    using (
        exists (
            select 1 from public.profiles
            where id = auth.uid()
              and role = 'admin'
        )
    );


-- ============================================================
-- C. Indexes
-- ============================================================

-- Idempotency: fast lookup by file hash (ingest worker deduplication)
create index if not exists idx_docs_file_hash
    on public.docs (file_hash);

-- Ingest queue: find docs by processing status
create index if not exists idx_docs_status
    on public.docs (status);

-- API default sort: newest first
create index if not exists idx_docs_created_at
    on public.docs (created_at desc);


-- ============================================================
-- D. updated_at trigger (reuse pattern from conversations)
-- ============================================================

create or replace function public.fn_touch_doc()
    returns trigger
    language plpgsql
    security definer
    set search_path = public
as $$
begin
    new.updated_at = now();
    return new;
end;
$$;

drop trigger if exists trg_docs_updated_at on public.docs;

create trigger trg_docs_updated_at
    before update on public.docs
    for each row
    execute function public.fn_touch_doc();


-- ============================================================
-- E. Storage bucket RLS
--
-- Run AFTER creating the medical_docs bucket in the dashboard.
-- Supabase Dashboard → Storage → medical_docs → Policies
-- (or paste this block after bucket creation)
-- ============================================================

-- Any authenticated user can read/download documents
drop policy if exists "medical_docs: authenticated read"  on storage.objects;
create policy "medical_docs: authenticated read"
    on storage.objects for select
    to authenticated
    using (bucket_id = 'medical_docs');

-- Only admins can upload
drop policy if exists "medical_docs: admin upload"  on storage.objects;
create policy "medical_docs: admin upload"
    on storage.objects for insert
    to authenticated
    with check (
        bucket_id = 'medical_docs'
        and exists (
            select 1 from public.profiles
            where id = auth.uid()
              and role = 'admin'
        )
    );

-- Only admins can delete from storage
drop policy if exists "medical_docs: admin delete"  on storage.objects;
create policy "medical_docs: admin delete"
    on storage.objects for delete
    to authenticated
    using (
        bucket_id = 'medical_docs'
        and exists (
            select 1 from public.profiles
            where id = auth.uid()
              and role = 'admin'
        )
    );

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
