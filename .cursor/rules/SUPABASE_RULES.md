# Supabase Rules (DB/Auth/RLS/Storage)

## Security & access control
- Supabase is the **source of truth** for authentication and database permissions.
- **RLS must be enabled** on any table that contains user-related data (e.g., profiles, conversations, messages, document metadata).
- Default row ownership rules:
  - user can read/write rows only where `user_id = auth.uid()`
  - profiles keyed by auth user: `profiles.id = auth.uid()`

## Keys & environment variables
- Allowed in the frontend (public):
  - `NEXT_PUBLIC_SUPABASE_URL`
  - `NEXT_PUBLIC_SUPABASE_ANON_KEY`
- **Forbidden in the frontend**:
  - `SUPABASE_SERVICE_ROLE_KEY`
  - any LLM API key (Anthropic/OpenAI/etc.)
- Backend uses `SUPABASE_SERVICE_ROLE_KEY` only server-side.

## Data modeling guidelines
- Prefer UUIDs.
- Recommended minimal model:
  - `profiles.id` is PK and FK to `auth.users.id`
  - `conversations.user_id` references `auth.users.id`
  - `messages.user_id` references `auth.users.id`
  - `messages.conversation_id` references `conversations.id` (with cascade delete)

## RLS policy expectations
- `profiles`: user can SELECT/INSERT/UPDATE only their own row (`id = auth.uid()`).
- `conversations`: user can SELECT/INSERT/UPDATE/DELETE only where `user_id = auth.uid()`.
- `messages`: user can SELECT/INSERT/UPDATE/DELETE only where `user_id = auth.uid()`.
- Enforce inserts so that `messages.user_id` must match `auth.uid()`.

## RAG / vectors
- Store chunks in a `doc_chunks` table with:
  - chunk text
  - metadata (doc id, title, section, timestamps)
  - embedding vector (pgvector)
- Retrieval must return chunk IDs + metadata so the assistant can cite sources.

## Storage
- Store documents in Supabase Storage; keep file metadata in DB.
- Ingest reads from Storage and writes chunk rows to the vector table.

## Operational notes
- Prefer explicit indexes for common filters:
  - `conversations.user_id`
  - `messages.conversation_id`
  - doc chunk lookup fields (doc_id, updated_at, etc.)
