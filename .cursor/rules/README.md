# Cursor Rules

## Active rule files (`.mdc` — Cursor native format with frontmatter)

| File | Scope | Always active |
|---|---|---|
| `backend.mdc` | `ingest/**/*.py` | No — triggered by file glob |
| `frontend.mdc` | `web/**/*.{ts,tsx}` | No — triggered by file glob |
| `security.mdc` | `**/*` | Yes |
| `clinical-safety.mdc` | `**/*` | Yes |

These files contain the authoritative standards. They take precedence over the `.md` reference files below.

## Reference files (`.md` — background documentation)

| File | Topic |
|---|---|
| `GLOBAL_RULES.md` | Cross-cutting engineering principles |
| `AI_AGENT_RULES.md` | AI output safety behaviour |
| `SUPABASE_RULES.md` | DB / RLS / key handling |
| `API_RULES.md` | FastAPI orchestrator patterns |
| `INGEST_RULES.md` | Document ingest worker |
| `PROMPTING_RULES.md` | Prompt versioning and contracts |
| `PRISMA_POLICY.md` | Explicit no-Prisma decision |
| `WEB_RULES.md` | Next.js / Vercel |

## Architecture note

Active backend is `/ingest` (FastAPI, port 8000). References to `/api` in the `.md` files describe a
planned service not yet created — ignore those references until that milestone begins.
