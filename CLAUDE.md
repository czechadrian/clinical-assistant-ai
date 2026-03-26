# Kliniczny Asystent AI

<project>
Clinical decision-support assistant for Polish-speaking healthcare professionals.
Medical context: responses affect clinical decisions — correctness, privacy, and safety are non-negotiable.

Status: Week 1 complete (auth, conversations, messages, mock LLM, PII + unsafe + vague guardrails, RLS, tests).
Next milestone: real Claude API integration (Week 2).
</project>

---

<architecture>

```
/ingest   FastAPI backend (Python 3.11, uv)     → port 8000
/web      Next.js 16 App Router (TypeScript)     → port 3000
Supabase  Auth + Postgres + RLS                  → no direct DB connections in app code
```

Data flow: `frontend → FastAPI (JWT validated) → PostgREST (RLS enforced) → Postgres`
Auth: Supabase JWT issued on login, validated on every backend request via `/auth/v1/user`.
The frontend **never** touches the DB directly for chat — always through FastAPI.
</architecture>

---

<commands>

### Backend
```bash
uv run --project ingest uvicorn main:app --reload --app-dir ingest   # dev server
uv run --project ingest pytest ingest/tests/ -v                      # tests (currently 6)
uv run --project ingest ruff check ingest/                           # lint
uv run --project ingest ruff format ingest/                          # format
```

### Frontend
```bash
cd web && pnpm dev
pnpm exec tsc --noEmit    # type-check — run before considering any TS change done
pnpm lint
```
</commands>

---

<key_files>

| Path | Purpose |
|---|---|
| `ingest/main.py` | All FastAPI endpoints, `Auth`, DB helpers, payload builders |
| `ingest/settings.py` | `Settings.from_env()` + `JsonFormatter` + structured logging |
| `ingest/guardrails.py` | `detect_pii`, `detect_unsafe_request`, `is_vague_input` |
| `ingest/policy.py` | `SYSTEM_PROMPT` + `PROMPT_VERSION` — bump version on every prompt change |
| `ingest/tests/conftest.py` | Fake env + `get_auth` override; never hits real Supabase |
| `ingest/tests/test_chat.py` | 6 smoke tests covering all guardrail paths |
| `web/lib/api.ts` | All typed fetch calls + `NoSessionError`, `ApiError`, retry on 401 |
| `web/components/MessagePanel.tsx` | Chat UI: composer, mode selector, templates, optimistic update |
| `web/components/ConversationList.tsx` | Left sidebar; re-exports `Conversation = ConversationOut` |
| `supabase/schema.sql` | Canonical schema: tables, RLS policies, indexes, trigger (idempotent) |
</key_files>

---

<env_vars>

### Backend (`ingest/.env`)
```
SUPABASE_URL=https://<project>.supabase.co
SUPABASE_SERVICE_ROLE_KEY=<key>   # project-ID only — NEVER pass to frontend
ANTHROPIC_API_KEY=<key>           # wired in Week 2
CHAT_MOCK_MODE=true               # false → 501 Not Implemented
APP_ENV=local                     # local | production
ALLOWED_ORIGINS=http://localhost:3000
```

### Frontend (`web/.env.local`)
```
NEXT_PUBLIC_SUPABASE_URL=https://<project>.supabase.co
NEXT_PUBLIC_SUPABASE_ANON_KEY=<anon_key>   # safe for browser
NEXT_PUBLIC_API_BASE_URL=http://localhost:8000
```
</env_vars>

---

<rules>

### Python / FastAPI

- Always run `ruff check` + `ruff format` after any backend change.
- Type-annotate all function signatures. Use `str | None` (union syntax), never `Optional[str]`.
- Pydantic models are the source of truth for request/response shapes — validate at the boundary, trust internally.
- Never read `os.environ` in `main.py` — all env access goes through `settings.py`.
- All DB access via `_db_insert` / `_db_select` helpers — never call `_db_client` directly in endpoints.
- Inject user JWT per-request via `_rls_headers(jwt)` — never set `Authorization` on the shared `_db_client`.
- New endpoints must use `Auth = Depends(get_auth)` — `get_supabase_user` is legacy (only `/whoami`).
- Never derive `user_id` from the request body — always use `auth.user_id` from the validated `Auth` dependency.
- Log only safe fields: `request_id`, `user_id`, `conversation_id`, `mode`, `input_length`, `latency_ms`, `status_code`, `is_mock`, `prompt_version`. Never log `input_text` or any assistant content.
- Guardrail functions must be side-effect-free so they are testable without FastAPI.

### TypeScript / Next.js

- Run `pnpm exec tsc --noEmit` before marking any TypeScript change as done.
- All backend calls go through `apiFetch` in `web/lib/api.ts` — never call `fetch()` directly in components.
- `Conversation` in components is a re-export of `ConversationOut` from `api.ts` — no duplicate type definitions.
- Catch `NoSessionError` at the page level and redirect to `/login`. Surface `ApiError` in the UI.
- Always return a cleanup function from async `useEffect` — `() => { cancelled = true }` prevents stale state.
- No direct Supabase DB calls from components for chat data — always go through FastAPI.

### Testing

- Every new guardrail behaviour needs a corresponding test in `test_chat.py`.
- Patch `_db_select` / `_db_insert` with `AsyncMock` — tests never touch a real database.
- `conftest.py` overrides `get_auth` — tests never need a real JWT or Supabase connection.
- After adding tests, confirm the full suite passes: `uv run --project ingest pytest ingest/tests/ -v`.

</rules>

---

<response_schema>

Every `/chat` response must include a valid `AssistantPayload`:
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

`ChatResponse` always includes `response_metadata: { is_mock, model, prompt_version }`.

`/chat` routing order:
1. PII detected → `400 Bad Request`
2. `CHAT_MOCK_MODE=false` → `501 Not Implemented`
3. Conversation not owned by user → `403 Forbidden`
4. Unsafe request (injection / out-of-scope) → `200` with `flag="refuse"`
5. Vague input (< 5 words) → `200` with `flag="uncertain"` + clarifying questions
6. Normal → `200` with mode-specific payload
</response_schema>

---

<forbidden>

- **No service role key bypass** — always pass user JWT so PostgREST evaluates RLS as that user.
- **No patient text in logs** — `input_text` and assistant content are permanently off-limits.
- **No Prisma** — schema is owned by `supabase/schema.sql`. Prisma would require duplicating RLS in app code.
- **No tRPC** — backend is Python/FastAPI, not Node.js.
- **No real LLM calls yet** — `CHAT_MOCK_MODE=false` returns 501 intentionally. Week 2 only.
- **No `user_id` from request body** — always derive from `Auth.user_id` (validated server-side).
- **No `NEXT_PUBLIC_*` for secrets** — the service role key must never reach the browser bundle.
- **No direct DB queries for chat from the frontend** — always route through FastAPI for RLS enforcement.
</forbidden>

---

<db_schema>

Three tables in `public`:
- `profiles (id, email)` — one row per auth user; upserted on login
- `conversations (id, user_id, title, created_at, updated_at)` — `updated_at` bumped by trigger on message insert
- `messages (id, conversation_id, user_id, role, content jsonb, created_at)`

RLS enabled on all three. Every policy: `auth.uid() = user_id`.
Apply schema: paste `supabase/schema.sql` into Supabase SQL editor (idempotent).
</db_schema>

---

<week2_checklist>

- [ ] Add `anthropic>=0.25` to `ingest/pyproject.toml` dependencies
- [ ] Replace `_build_mock_payload` with real `anthropic.messages.create` call using `SYSTEM_PROMPT`
- [ ] Validate Claude's JSON output into `AssistantPayload` via Pydantic (catch `ValidationError`)
- [ ] Set `is_mock=False`, `model="claude-opus-4-6"` in `ResponseMetadata`
- [ ] Set `CHAT_MOCK_MODE=false` in production env
- [ ] Increment `PROMPT_VERSION` in `policy.py` when `SYSTEM_PROMPT` changes
- [ ] Add `/chat/stream` SSE endpoint for streaming responses
- [ ] Rate limiting via `slowapi`
</week2_checklist>
