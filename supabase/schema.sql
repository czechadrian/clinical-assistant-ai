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
