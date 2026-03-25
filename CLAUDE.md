# Kliniczny Asystent AI — Claude Code Guide

Clinical decision-support assistant for Polish-speaking healthcare professionals.
**Week 1 complete** (Days 1–6): auth, conversations, messages, mock LLM, PII guardrail, RLS.
**Next milestone**: real Claude API integration (replace mock in `/ingest/main.py`).

---

## Architecture

```
/ingest   FastAPI backend (Python 3.11, uv)          → port 8000
/web      Next.js 16 App Router (TypeScript, pnpm)   → port 3000
Supabase  Auth + Postgres + RLS (no direct Postgres connection)
```

The frontend never touches the DB directly for chat — it calls the FastAPI backend.
Auth is via Supabase JS client; the backend validates the Supabase JWT on every request.

---

## Commands

### Backend
```bash
# Dev server
uv run --project ingest uvicorn main:app --reload --app-dir ingest

# Tests
uv run --project ingest pytest ingest/tests/ -v

# Lint + format check
uv run --project ingest ruff check ingest/
uv run --project ingest ruff format --check ingest/
uv run --project ingest ruff format ingest/   # auto-fix
```

### Frontend
```bash
cd web
pnpm dev
pnpm exec tsc --noEmit    # type check
pnpm lint
```

---

## Environment variables

### Backend (`ingest/.env`)
```
SUPABASE_URL=https://<project>.supabase.co
SUPABASE_SERVICE_ROLE_KEY=<service_role_key>   # never expose to frontend
ANTHROPIC_API_KEY=<key>                         # unused until Week 2
CHAT_MOCK_MODE=true                             # false → 501 Not Implemented
APP_ENV=local                                   # local | production
ALLOWED_ORIGINS=http://localhost:3000
```

### Frontend (`web/.env.local`)
```
NEXT_PUBLIC_SUPABASE_URL=https://<project>.supabase.co
NEXT_PUBLIC_SUPABASE_ANON_KEY=<anon_key>       # public, safe for browser
NEXT_PUBLIC_API_BASE_URL=http://localhost:8000
```

---

## Key files

| Path | Purpose |
|---|---|
| `ingest/main.py` | All FastAPI endpoints, auth dependency, DB helpers |
| `ingest/settings.py` | `Settings.from_env()` + structured JSON logging |
| `ingest/guardrails.py` | PII detection (email, phone, PESEL, NIP) |
| `ingest/policy.py` | `SYSTEM_PROMPT` + `PROMPT_VERSION` constant |
| `ingest/tests/` | pytest smoke tests; conftest sets fake env vars |
| `web/lib/api.ts` | All typed fetch calls to the backend |
| `web/components/MessagePanel.tsx` | Main chat UI: composer, mode selector, templates |
| `web/components/ConversationList.tsx` | Left sidebar |
| `supabase/schema.sql` | Canonical schema: tables, RLS policies, indexes, trigger |
| `supabase/test_rls.sh` | Curl-based RLS smoke test |

---

## Critical conventions

### Security / RLS
- The backend uses the service role key **only as `apikey`** (API-gateway project ID).
- The user's JWT is injected per-request as `Authorization: Bearer <jwt>` so PostgREST enforces RLS.
- Never trust `user_id` from the request body — always use the validated `Auth.user_id`.
- Never expose `SUPABASE_SERVICE_ROLE_KEY` to any frontend code.

### Privacy / logging
- Never log `input_text`, assistant content, or any patient data.
- Log only: `request_id`, `user_id`, `conversation_id`, `mode`, `input_length`, `latency_ms`, `status_code`, `is_mock`, `prompt_version`.
- PII guardrail runs **before any DB write** in `/chat`.

### Response schema
Every `/chat` response must return a valid `AssistantPayload`:
```json
{
  "questions_to_ask": [],
  "red_flags": [],
  "possible_next_steps": [],
  "patient_facing_summary": "",
  "sources": [{"id": "", "title": "", "section": ""}],
  "flag": "safe | uncertain | refuse",
  "disclaimer": ""
}
```

### AI metadata
Every `ChatResponse` includes `response_metadata: {is_mock, model, prompt_version}`.
When replacing the mock with a real Claude call, set `is_mock=False` and `model="claude-opus-4-6"`.
Increment `PROMPT_VERSION` in `ingest/policy.py` whenever `SYSTEM_PROMPT` changes.

---

## What NOT to do

- **No Prisma** — schema is owned by `supabase/schema.sql` + PostgREST. Prisma would require duplicating RLS in application code.
- **No tRPC** — backend is Python/FastAPI, not Node.js.
- **No service role key bypass** — always pass user JWT; let RLS do its job.
- **No LLM calls yet** — `CHAT_MOCK_MODE=false` returns 501 intentionally. Week 2 only.
- **No raw patient text in logs** — ever.

---

## DB schema summary

Three tables in `public`:
- `profiles (id, email)` — one row per Supabase auth user; upserted on login
- `conversations (id, user_id, title, created_at, updated_at)` — `updated_at` bumped by trigger on message insert
- `messages (id, conversation_id, user_id, role, content jsonb, created_at)`

RLS is enabled on all three. Every policy filters by `auth.uid() = user_id`.

To apply the schema: run `supabase/schema.sql` in the Supabase SQL editor (idempotent).

---

## Week 2 checklist (not started)

- [ ] Wire `anthropic` SDK: replace `_build_mock_payload` with real `messages.create` call
- [ ] Set `CHAT_MOCK_MODE=false` in production env
- [ ] Set `response_metadata.is_mock=False`, `model="claude-opus-4-6"`
- [ ] Add streaming support to `/chat` (SSE or chunked)
- [ ] Rate limiting (`slowapi` for FastAPI)
