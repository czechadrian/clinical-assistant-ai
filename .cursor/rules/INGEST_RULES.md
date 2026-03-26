# Ingest Rules (Worker)

## Separation of concerns
- `/ingest` is a separate Python project with its own dependencies.
- Keep ingest logic independent from API runtime where possible.

## Pipeline (idempotent)
1) Read documents from Supabase Storage (or a list of doc references from DB).
2) Extract text.
3) Chunk text (stable chunking strategy).
4) Create embeddings.
5) Write `doc_chunks` rows (text + metadata + embedding).

## Idempotency & deduplication
- Rerunning ingest must not create duplicates.
- Use a document hash/version, and store it with metadata.
- Update/replace chunks when the source document changes.

## Scheduling
- Trigger ingest:
  - manually during development
  - automatically via Supabase Cron/Queues in production

## Observability
- Log per-run metadata:
  - doc id
  - chunk count
  - elapsed time
  - failures (with doc id, not raw content)
