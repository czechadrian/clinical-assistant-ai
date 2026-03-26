# Global Project Rules

## Core principles
- Prefer **simple, production-minded defaults**. Avoid unnecessary libraries and “clever” architecture.
- Use a **monorepo** structure:
  - `/web` — Next.js (UI)
  - `/api` — FastAPI (agent/orchestrator)
  - `/ingest` — Python worker (document ingest)
- Every change must be **testable and reversible**:
  - version prompts,
  - script DB migrations,
  - maintain a small golden set of test cases.
- Never assume success: include **error handling**, **timeouts**, **retries only when fixable**, and **clear logs**.

## Security baseline
- Treat the frontend as hostile; never trust client claims.
- Keep secrets server-side only (LLM keys, service role keys).
- Store only what you need; minimize sensitive content retention.

## Quality baseline
- Use structured outputs where possible (JSON + validation).
- Add regression tests for prompts and agent behavior.
- Prefer “fail safe” over “best-effort guess” in medical contexts.
