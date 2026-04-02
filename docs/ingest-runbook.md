# Ingest Worker Runbook

Operations guide for the Kliniczny Asystent AI ingest pipeline.

---

## Overview

The ingest pipeline converts medical guideline documents (PDF, TXT, MD) into
vector embeddings stored in the `doc_chunks` table. The `/chat` endpoint uses
these chunks for retrieval-augmented generation (RAG).

**Document lifecycle:**

```
pending → processing → ready
                   └→ failed
```

| Status | Meaning |
|---|---|
| `pending` | Uploaded but not yet processed |
| `processing` | Worker has claimed the document and is actively ingesting |
| `ready` | Successfully chunked, embedded, and stored |
| `failed` | Worker encountered an unrecoverable error — see `last_error_code` |
| `indexed` | Legacy status from worker v1 — treated as `ready` |

---

## Triggering ingest

### Manual (one-off)

```bash
# Process all pending documents
uv run python worker.py --all

# Force re-process a specific document (even if already indexed)
uv run python worker.py --doc-id <uuid>
```

### Via API (admin role required)

```bash
curl -X POST https://YOUR-API-URL/admin/ingest \
  -H "Authorization: Bearer YOUR-JWT" \
  -H "Content-Type: application/json"
```

Response `202 Accepted`:
```json
{ "queued": 3, "message": "Queued 3 document(s) for processing." }
```

Response `409 Conflict` — already running:
```json
{ "error": { "code": "INGEST_RUNNING", "message": "..." } }
```

### Automated (Supabase Cron)

See `supabase/migrations/20260402_ingest_automation.sql` for setup SQL.
Runs nightly at 02:00 UTC. Verify with:

```sql
SELECT jobid, jobname, schedule, active FROM cron.job;
```

Monitor run history:

```sql
SELECT start_time, end_time, status, return_message
FROM   cron.job_run_details
ORDER  BY start_time DESC
LIMIT  20;
```

---

## Checking ingest status

### Admin UI

Navigate to `/admin/docs` in the web app. Status badges update every 5 seconds
while any document is in `processing` state.

### API

```bash
curl https://YOUR-API-URL/admin/ingest/status \
  -H "Authorization: Bearer YOUR-JWT"
```

```json
{ "counts": { "ready": 12, "pending": 1, "failed": 0 }, "total": 13 }
```

### Database (direct)

```sql
SELECT status, count(*) FROM docs GROUP BY status;

-- See details for failed documents
SELECT id, title, last_error_code, updated_at
FROM   docs
WHERE  status = 'failed'
ORDER  BY updated_at DESC;
```

---

## Troubleshooting

### Doc stuck in `processing`

**Symptom:** A document stays in `processing` for more than 10 minutes and
no new `doc_ingest_done` log appears.

**Cause:** The worker process was killed mid-run (OOM, timeout, deploy restart).

**Fix via API (admin):**

```bash
curl -X POST https://YOUR-API-URL/admin/ingest/reset-stuck \
  -H "Authorization: Bearer YOUR-JWT"
```

**Fix via SQL (direct access):**

```sql
UPDATE docs SET status = 'pending' WHERE status = 'processing';
```

Then re-trigger ingest.

---

### Doc in `failed` state

**Check the error:**

```sql
SELECT id, title, last_error_code, updated_at
FROM   docs WHERE status = 'failed';
```

| Error pattern | Likely cause | Fix |
|---|---|---|
| `Text extraction produced no content` | Scanned/image-only PDF | Re-upload a text-selectable PDF |
| `Unsupported file type` | Wrong file extension | Re-upload as `.pdf`, `.txt`, or `.md` |
| `Embedder returned N vectors for M chunks` | OpenAI API partial failure | Retry: reset to `pending`, re-trigger |
| `HTTP 401` or `HTTP 403` | Service role key misconfigured | Check `SUPABASE_SERVICE_ROLE_KEY` env var |
| `HTTP 503` / connection error | Supabase unreachable | Check Supabase project status, retry later |
| `OPENAI_API_KEY is not set` | Missing API key | Set `OPENAI_API_KEY` in environment |

**Retry a failed document:**

```sql
-- Reset one document
UPDATE docs SET status = 'pending', last_error_code = NULL WHERE id = '<uuid>';
```

Then trigger ingest via API or CLI.

---

### No chunks found after `ready`

**Symptom:** Document is `ready` but `/retrieve` returns no results for related queries.

**Check chunks exist:**

```sql
SELECT count(*) FROM doc_chunks WHERE doc_id = '<uuid>';
```

If 0 rows: the ingest completed but writing chunks failed silently (unlikely but
possible if a previous version of the worker had a bug). Fix:

```bash
uv run python worker.py --doc-id <uuid>
```

This always re-processes regardless of current status and deletes old chunks
before inserting new ones (idempotent).

---

### `OPENAI_API_KEY not configured` error (503)

```bash
# Check env
echo $OPENAI_API_KEY

# Set in .env
echo "OPENAI_API_KEY=sk-..." >> ingest/.env
```

---

### Cron not firing

1. Verify pg_cron is enabled: `SELECT * FROM pg_extension WHERE extname = 'pg_cron';`
2. Verify the job exists: `SELECT * FROM cron.job;`
3. Check run history: `SELECT * FROM cron.job_run_details ORDER BY start_time DESC LIMIT 5;`
4. Check that the API URL is reachable from Supabase's network
5. Verify the service role key used in the cron header is current

---

## Observability

All worker events emit structured JSON logs. Key events:

| Event | Fields |
|---|---|
| `doc_ingest_start` | `doc_id`, `filename` |
| `doc_embedding_start` | `doc_id`, `chunk_count` |
| `doc_ingest_done` | `doc_id`, `filename`, `file_hash`, `chunk_count`, `elapsed_ms`, `status` |
| `doc_ingest_failed` | `doc_id`, `filename`, `elapsed_ms`, `error` |
| `doc_status_update_failed` | `doc_id` |
| `no_pending_docs` | _(none)_ |
| `ingest_already_running` | `processing_count` |
| `run_all_start` | `doc_count` |
| `run_all_done` | `doc_count`, `failed`, `succeeded` |
| `reset_stuck_docs` | `count`, `user_id` |

Filter logs in production:

```bash
# All ingest events for a specific document
grep '"doc_id": "<uuid>"' app.log | jq .

# All failures today
grep '"msg": "doc_ingest_failed"' app.log | jq '{doc_id, error, elapsed_ms}'
```

---

## G) Checkpoints — verify each piece worked

```bash
# 1. Migration applied
uv run --project ingest psql $DATABASE_URL -c \
  "SELECT column_name FROM information_schema.columns WHERE table_name='docs';" | grep last_

# 2. Worker transitions correctly (unit tests)
uv run --project ingest pytest ingest/tests/test_ingest.py -v -k worker

# 3. All tests pass
uv run --project ingest pytest ingest/tests/ -v

# 4. Ruff clean
uv run --project ingest ruff check ingest/

# 5. TypeScript types check
cd web && pnpm exec tsc --noEmit

# 6. Manual worker run (needs OPENAI_API_KEY + pending doc)
uv run python ingest/worker.py --all

# 7. Admin API endpoint (local)
curl -X GET http://localhost:8000/admin/ingest/status \
  -H "Authorization: Bearer YOUR-JWT"

# 8. Cron schedule (production, in Supabase SQL Editor)
SELECT jobid, jobname, schedule, active FROM cron.job;
```
