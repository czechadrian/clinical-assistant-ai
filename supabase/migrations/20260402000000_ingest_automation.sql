-- ============================================================
-- Migration: ingest automation — 2026-04-02
--
-- Adds:
--   - 'processing' and 'ready' status values to docs.status
--     ('indexed' kept for backward compatibility with existing rows)
--   - last_ingested_at  timestamptz — set on each successful ingest
--   - last_error_code   text        — short error description on failure
--
-- Idempotent: safe to re-run on an existing database.
-- ============================================================


-- ── A. Status check constraint ────────────────────────────────────────────────
--
-- Old: ('pending', 'indexed', 'failed')
-- New: ('pending', 'processing', 'ready', 'indexed', 'failed')
--
-- 'processing' — worker has claimed the document and is actively ingesting it.
-- 'ready'      — worker v2 success status (replaces 'indexed' for new runs).
-- 'indexed'    — kept so existing rows remain valid; treated as 'ready' in queries.

ALTER TABLE public.docs
    DROP CONSTRAINT IF EXISTS docs_status_check;

ALTER TABLE public.docs
    ADD CONSTRAINT docs_status_check
    CHECK (status IN ('pending', 'processing', 'ready', 'indexed', 'failed'));


-- ── B. New columns ────────────────────────────────────────────────────────────

ALTER TABLE public.docs
    ADD COLUMN IF NOT EXISTS last_ingested_at timestamptz,
    ADD COLUMN IF NOT EXISTS last_error_code  text;


-- ── C. Supabase Cron automation (OPTIONAL) ────────────────────────────────────
--
-- Runs the ingest worker nightly at 02:00 UTC by calling POST /admin/ingest.
-- Requires the pg_cron and pg_net extensions (available on all Supabase plans).
--
-- ── Setup (run once in Supabase SQL Editor) ───────────────────────────────────
--
--   CREATE EXTENSION IF NOT EXISTS pg_cron;
--   CREATE EXTENSION IF NOT EXISTS pg_net;
--
-- ── Schedule (replace placeholders, then run) ─────────────────────────────────
--
--   SELECT cron.schedule(
--       'nightly-ingest',
--       '0 2 * * *',          -- 02:00 UTC every day
--       $$
--       SELECT net.http_post(
--           url      := 'https://YOUR-API-URL/admin/ingest',
--           headers  := jsonb_build_object(
--               'Authorization', 'Bearer YOUR-SERVICE-ROLE-KEY',
--               'Content-Type',  'application/json'
--           ),
--           body     := '{}'::jsonb
--       )
--       $$
--   );
--
-- ── Verify ────────────────────────────────────────────────────────────────────
--
--   SELECT jobid, jobname, schedule, active FROM cron.job;
--
-- ── Monitor runs ──────────────────────────────────────────────────────────────
--
--   SELECT start_time, end_time, status, return_message
--   FROM   cron.job_run_details
--   ORDER  BY start_time DESC
--   LIMIT  20;
--
-- ── Remove schedule ───────────────────────────────────────────────────────────
--
--   SELECT cron.unschedule('nightly-ingest');
