-- ============================================================
-- Migration 002 — conversation_state
--
-- Adds a one-row-per-conversation memory table that stores a
-- short, sanitized clinical-context summary (no identifiers).
--
-- Safe to run on an existing database (idempotent).
-- ============================================================


-- ---- Table ----

create table if not exists public.conversation_state (
  -- Primary key is conversation_id: exactly one state row per conversation.
  -- Cascade delete: state is removed when the conversation is deleted.
  conversation_id   uuid        primary key
                    references  public.conversations(id) on delete cascade,

  -- Denormalised for fast RLS evaluation without joining conversations.
  user_id           uuid        not null
                    references  auth.users(id) on delete cascade,

  -- Sanitized clinical context — no patient identifiers.
  -- Built from assistant-generated fields only (never raw user text).
  summary           text        not null default '',

  -- Last set of clarifying questions the assistant posed.
  open_questions    jsonb       not null default '[]'::jsonb,

  -- Red-flag labels or notable constraints identified during triage.
  known_constraints jsonb       not null default '[]'::jsonb,

  updated_at        timestamptz not null default now()
);


-- ---- Row Level Security ----

alter table public.conversation_state enable row level security;

drop policy if exists "conversation_state: select own"  on public.conversation_state;
drop policy if exists "conversation_state: insert own"  on public.conversation_state;
drop policy if exists "conversation_state: update own"  on public.conversation_state;
drop policy if exists "conversation_state: delete own"  on public.conversation_state;

-- Owners read their own state rows.
create policy "conversation_state: select own"
  on public.conversation_state for select
  using (auth.uid() = user_id);

-- INSERT: user_id must match the caller AND the target conversation must
-- also belong to that user.  Prevents writing state for another user's
-- conversation even if the conversation_id is known.
create policy "conversation_state: insert own"
  on public.conversation_state for insert
  with check (
    auth.uid() = user_id
    and exists (
      select 1 from public.conversations c
      where  c.id      = conversation_id
        and  c.user_id = auth.uid()
    )
  );

-- UPDATE: both USING and WITH CHECK require ownership.
create policy "conversation_state: update own"
  on public.conversation_state for update
  using     (auth.uid() = user_id)
  with check (auth.uid() = user_id);

-- DELETE: cascade from conversations handles this in practice.
create policy "conversation_state: delete own"
  on public.conversation_state for delete
  using (auth.uid() = user_id);


-- ---- Indexes ----

-- Supports listing all state rows for a user (admin / debug queries).
create index if not exists idx_conversation_state_user_id
  on public.conversation_state (user_id);
