# Kliniczny Asystent AI

A clinical decision-support assistant for Polish-speaking healthcare professionals. The system helps clinicians with patient triage, visit summaries, and patient-facing communication by providing structured, evidence-based suggestions grounded in indexed medical guidelines.

> **Scope.** The assistant presents possibilities, flags red-flag symptoms, and asks clarifying questions. It never issues a definitive diagnosis, never prescribes medication, and always appends a mandatory safety disclaimer. Correctness, privacy, and safety are non-negotiable constraints at every layer of the stack.

---

## Features

- **Three consultation modes** — triage, visit summary, patient message
- **RAG-powered responses** — answers grounded in indexed guideline documents (PTK, ESC, WHO, PTD, etc.)
- **Input guardrails** — PII detection (PESEL, NIP, phone, e-mail), injection-attempt flagging, vague-input detection
- **Structured output** — every response is a validated JSON payload: `questions_to_ask`, `red_flags`, `possible_next_steps`, `patient_facing_summary`, `sources`, `flag`, `disclaimer`
- **Idempotent requests** — `Idempotency-Key` header prevents duplicate messages on network retries
- **Audit trail** — every message stores `prompt_version`, `model`, `is_mock`, and classification metadata; raw patient text is never logged
- **Document ingest pipeline** — PDF / TXT / MD → chunked → embedded → stored with pgvector for semantic search

---

## Architecture

```
┌─────────────────────┐     JWT      ┌──────────────────────┐     PostgREST + RLS    ┌─────────────┐
│   Next.js 16        │ ──────────▶  │   FastAPI (Python)   │ ─────────────────────▶ │  Supabase   │
│   App Router        │             │   port 8000           │                        │  Postgres   │
│   port 3000         │ ◀────────── │   guardrails          │ ◀───────────────────── │  pgvector   │
└─────────────────────┘  ChatResponse│   validation          │                        │  Storage    │
                                     │   RAG retrieval       │                        └─────────────┘
                                     └──────────────────────┘
                                               │
                                               │  embed (OpenAI)  /  generate (Anthropic)
                                               ▼
                                     ┌──────────────────────┐
                                     │   External AI APIs   │
                                     │   text-embedding-3-  │
                                     │   small (1536 dims)  │
                                     │   claude-opus-4-6    │
                                     └──────────────────────┘
```

**Data flow:**
1. Frontend authenticates via Supabase Auth and obtains a JWT
2. All chat requests go `Frontend → FastAPI` — the frontend never touches the database directly
3. FastAPI validates the JWT against Supabase Auth on every request
4. Input is classified (PII / injection / unsafe / vague) before any DB write
5. Relevant guideline chunks are retrieved via pgvector cosine similarity
6. The LLM response is validated and repaired against `AssistantPayload` schema before storage
7. FastAPI writes to Postgres via PostgREST with the user's JWT — RLS enforces row-level isolation

---

## Stack

| Layer | Technology | Notes |
|---|---|---|
| Frontend | Next.js 16 App Router, TypeScript | `pnpm`, Tailwind CSS |
| Backend | FastAPI, Python 3.11, `uv` | Structured JSON logging, full-jitter retry |
| Database | Supabase (Postgres 15) | RLS on all tables, no direct DB from frontend |
| Vector search | pgvector, `vector(1536)` | `match_chunks` SQL function, cosine distance |
| Embeddings | OpenAI `text-embedding-3-small` | 1536 dims, batched in groups of 100 |
| LLM | Anthropic Claude (`claude-opus-4-6`) | Structured JSON output mode |
| Auth | Supabase Auth (JWT) | Validated server-side on every request |
| Storage | Supabase Storage | Private `medical_docs` bucket, service-role download |
| Validation | Pydantic v2 (backend) | One-pass repair before storage, never stores invalid output |

---

## Project Structure

```
/
├── ingest/                 # FastAPI backend + ingest worker
│   ├── main.py             # All endpoints, DB helpers, RAG pipeline
│   ├── settings.py         # Settings.from_env(), JsonFormatter, structured logging
│   ├── guardrails.py       # detect_pii, detect_unsafe_request, is_vague_input
│   ├── classifier.py       # classify_input() → ClassifiedInput (single-pass)
│   ├── validator.py        # validate_and_repair() — one Pydantic pass, no loops
│   ├── policy.py           # SYSTEM_PROMPT + PROMPT_VERSION
│   ├── chunker.py          # Sliding-window text chunker (pure function)
│   ├── embedder.py         # OpenAIEmbedder — batched async embeddings
│   ├── worker.py           # Ingest worker: download → extract → chunk → embed → store
│   └── tests/
│       ├── conftest.py     # Fake env, get_auth override, no real network
│       ├── test_chat.py    # Guardrail paths, idempotency, validation
│       ├── test_chunker.py # Chunker contract (16 tests)
│       ├── test_retrieve.py# /retrieve endpoint + RAG helpers (16 tests)
│       ├── test_classifier.py
│       ├── test_validator.py
│       ├── test_reliability.py
│       └── test_golden.py
│
├── web/                    # Next.js frontend
│   ├── app/                # App Router pages
│   ├── components/
│   │   ├── MessagePanel.tsx      # Chat UI, optimistic update, mode selector
│   │   └── ConversationList.tsx  # Sidebar
│   └── lib/
│       ├── api.ts          # apiFetch, withRetry, all typed API calls
│       └── supabaseClient.ts
│
└── supabase/
    ├── schema.sql          # profiles, conversations, messages + RLS (idempotent)
    ├── chunks_schema.sql   # doc_chunks + pgvector + RLS + indexes
    └── retrieve_fn.sql     # match_chunks() SQL function
```

---

## API Endpoints

| Method | Path | Auth | Description |
|---|---|---|---|
| `GET` | `/health` | — | Liveness check |
| `GET` | `/version` | — | Git commit + env |
| `GET` | `/whoami` | JWT | Resolved user identity |
| `POST` | `/conversations` | JWT | Create a conversation |
| `GET` | `/conversations` | JWT | List user's conversations |
| `GET` | `/conversations/{id}/messages` | JWT | Fetch message history |
| `POST` | `/chat` | JWT | Submit a clinical query, get structured response |
| `POST` | `/chat/stream` | JWT | SSE variant — streams delta tokens |
| `GET` | `/retrieve` | JWT | Semantic search over indexed guideline chunks |
| `GET` | `/docs` | JWT | List indexed documents |

### `POST /chat` request

```json
{
  "mode": "triage | summary | patient_message",
  "input_text": "string (1–2000 chars)",
  "conversation_id": "uuid"
}
```

### `POST /chat` response

```json
{
  "request_id": "uuid",
  "assistant_payload": {
    "questions_to_ask": ["string"],
    "red_flags": ["string"],
    "possible_next_steps": ["string"],
    "patient_facing_summary": "string",
    "sources": [{ "id": "string", "title": "string", "section": "string" }],
    "flag": "safe | uncertain | refuse",
    "disclaimer": "string"
  },
  "response_metadata": {
    "is_mock": true,
    "model": "mock-v1 | claude-opus-4-6",
    "prompt_version": "1.0.0"
  }
}
```

### Error shape

```json
{
  "error": {
    "code": "PII_DETECTED | MOCK_DISABLED | CONVERSATION_NOT_FOUND | VALIDATION_FAILED | TIMEOUT | TRANSIENT_UPSTREAM",
    "message": "string",
    "request_id": "uuid"
  }
}
```

---

## Ingest Worker

Downloads documents from Supabase Storage, extracts text, chunks with a sliding window, generates embeddings, and writes `doc_chunks` rows.

```bash
# Process one document (always re-processes, even if already indexed)
uv run python worker.py --doc-id <uuid>

# Process all documents with status='pending'
uv run python worker.py --all
```

**Pipeline:** download → extract text (PDF / TXT / MD) → normalise whitespace → sliding-window chunk (1000 chars, 100 overlap) → batch-embed (OpenAI) → delete old chunks → insert new chunks → mark `docs.status = 'indexed'`

Idempotent: delete-then-insert runs only after successful extraction. If extraction fails, old chunks are preserved.

---

## Local Development

### Prerequisites

- Python 3.11+, [`uv`](https://github.com/astral-sh/uv)
- Node.js 20+, `pnpm`
- A Supabase project with schema applied

### Backend

```bash
# Install dependencies
uv sync --project ingest

# Copy and fill environment variables
cp ingest/.env.example ingest/.env

# Run dev server
uv run --project ingest uvicorn main:app --reload --app-dir ingest

# Run tests (89 tests, no network required)
uv run --project ingest pytest ingest/tests/ -v

# Lint + format
uv run --project ingest ruff check ingest/
uv run --project ingest ruff format ingest/
```

### Frontend

```bash
cd web

# Install dependencies
pnpm install

# Copy and fill environment variables
cp .env.local.example .env.local

# Run dev server
pnpm dev

# Type-check
pnpm exec tsc --noEmit

# Lint
pnpm lint
```

### Apply database schema

Paste `supabase/schema.sql` then `supabase/chunks_schema.sql` then `supabase/retrieve_fn.sql` into the Supabase SQL editor. All files are idempotent.

---

## Environment Variables

### Backend (`ingest/.env`)

| Variable | Required | Description |
|---|---|---|
| `SUPABASE_URL` | Yes | Supabase project URL |
| `SUPABASE_SERVICE_ROLE_KEY` | Yes | Service role key — never expose to frontend |
| `ANTHROPIC_API_KEY` | Week 2 | Claude API key |
| `OPENAI_API_KEY` | Yes | Embedding API key |
| `EMBED_MODEL` | No | Embedding model (default: `text-embedding-3-small`) |
| `CHAT_MOCK_MODE` | No | `true` returns mock responses; `false` calls Claude (default: `true`) |
| `APP_ENV` | No | `local` or `production` — controls log verbosity (default: `local`) |
| `ALLOWED_ORIGINS` | No | Comma-separated CORS origins (default: `http://localhost:3000`) |
| `VALIDATION_DEBUG` | No | Expose field paths in validation errors — local dev only (default: `false`) |

### Frontend (`web/.env.local`)

| Variable | Required | Description |
|---|---|---|
| `NEXT_PUBLIC_SUPABASE_URL` | Yes | Supabase project URL |
| `NEXT_PUBLIC_SUPABASE_ANON_KEY` | Yes | Anon key — safe for browser |
| `NEXT_PUBLIC_API_BASE_URL` | No | Backend URL (default: `http://localhost:8000`) |

---

## Safety & Privacy

- **No patient text in logs.** `input_text` and assistant content are permanently off-limits. Only counts (`input_length`, `retrieved_count`, `injection_flag_count`) and identifiers (`request_id`, `user_id`) are logged.
- **PII detection blocks submission.** PESEL, NIP, phone numbers, and e-mail addresses are detected before any DB write and rejected with `400 Bad Request`.
- **Injection resistance at two layers.** Regex flags adversarial patterns server-side; retrieved chunks are wrapped in `<retrieved_context>` XML tags and the system prompt instructs the model to treat `<clinical_query>` content as data only.
- **RLS on every table.** PostgREST evaluates `auth.uid() = user_id` on every query. The service role key is never passed to the frontend.
- **Validated before storage.** Invalid LLM output is repaired once deterministically; if still invalid, the request fails with `500` — raw invalid content is never stored.

---

## Roadmap

- [x] Auth, conversations, messages, RLS
- [x] Input guardrails (PII, injection, unsafe, vague)
- [x] Mock LLM responses with validated `AssistantPayload`
- [x] Document ingest pipeline (PDF / TXT / MD)
- [x] pgvector semantic search + `GET /retrieve`
- [x] RAG context retrieval wired into `/chat` (`sources[]` populated)
- [ ] Real Claude API integration (`CHAT_MOCK_MODE=false`)
- [ ] Streaming responses via SSE (`/chat/stream`)
- [ ] Rate limiting (`slowapi`)
- [ ] Admin document upload UI
- [ ] HNSW index (after sufficient data volume)
- [ ] OpenAPI → TypeScript codegen (`openapi-typescript`)
