# API Rules (FastAPI Orchestrator)

## Responsibilities
- `/api` is the “brain” of the application:
  - token verification
  - guardrails and safety rules
  - intent routing
  - tool calling / retrieval orchestration
  - output validation (JSON)
  - logging and evaluation hooks

## Authentication & authorization
- Backend must verify the Supabase access token.
- Do not trust any `user_id` or role provided by the client.
- Use the verified user identity for:
  - access decisions
  - logging metadata
  - rate limiting and quotas

## Output discipline
- Prefer structured outputs (JSON).
- Validate server-side.
- If invalid output: one “repair retry” maximum; otherwise fail gracefully.

## Logging (privacy-first)
- Log metadata only:
  - request id
  - verified user id
  - prompt version
  - latency
  - token usage/cost
  - success/failure codes
- Avoid logging patient text.
- If a debug mode is introduced, it must be explicit, time-bounded, and sanitized.

## Reliability
- Implement timeouts for model calls and external services.
- Retry only errors that are likely transient and safe to retry.
- Keep error messages user-friendly; store details only in server logs.
