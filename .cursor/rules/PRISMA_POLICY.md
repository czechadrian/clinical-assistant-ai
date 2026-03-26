# Prisma Policy (Explicit Stance)

## Default position
- Default implementation is **Supabase-only**:
  - `supabase-js` for client interactions
  - RLS enforced in Postgres
  - FastAPI orchestrates the agent and validates identity

## When Prisma is optional
Prisma may be added if you need:
- TypeScript-first typed DB access on the server
- complex server-side relational queries or migrations in code
- a standard ORM layer across services

## Risk: bypassing RLS
- Using Prisma via direct DB connection can bypass RLS if not designed carefully.
- If Prisma is introduced, explicitly document how user-scoped authorization is enforced:
  - either keep user-scoped reads/writes through Supabase (recommended)
  - or implement a safe pattern to preserve RLS-like guarantees (advanced)

## Recommendation for MVP
- Weeks 1–3: stay Supabase-only.
- Reassess Prisma only after you have:
  - stable schemas
  - working RLS policies
  - tests/evals in place
