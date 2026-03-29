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
