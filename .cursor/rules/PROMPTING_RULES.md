# Prompting Rules (How to Ask the LLM While Building)

## Provide constraints up front
Include:
- stack: Next.js + Vercel, Supabase (Auth + RLS + pgvector), FastAPI, ingest worker
- secrets never in frontend
- RLS always enabled and used for user-scoped data
- minimal dependencies

## Ask for actionable outputs
Request:
- file structure
- exact commands
- checkpoints (“what should I see?”)
- common pitfalls and how to verify them

## Require verification-first behavior
- “Don’t guess; propose hypotheses and how to confirm.”
- “Prefer minimal changes before invasive refactors.”

## Keep solutions maintainable
- Avoid overengineering (multiple frameworks, complex agent stacks) unless necessary.
- Keep prompts versioned and testable (golden set + regression checks).
