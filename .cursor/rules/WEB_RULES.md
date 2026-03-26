# Web Rules (Next.js + Vercel)

## Hosting
- `/web` is deployed on **Vercel**.
- Use Vercel for UI and optional lightweight BFF routes, not for heavy agent orchestration by default.

## Security
- No LLM keys in browser code.
- No Supabase service role key in browser code.
- Only public Supabase variables are allowed client-side:
  - `NEXT_PUBLIC_SUPABASE_URL`
  - `NEXT_PUBLIC_SUPABASE_ANON_KEY`
- Protected routes:
  - if no session → redirect to `/login`.

## Auth flow
- Frontend obtains Supabase session access token.
- Every request to `/api` includes:
  - `Authorization: Bearer <access_token>`

## UX guidelines for clinicians
- Optimize for speed and clarity:
  - streaming responses,
  - clear sections (questions, red flags, next steps),
  - “edit before use” and “copy” actions.
- Provide visible limitations:
  - assistant supports clinical work but does not replace judgment.
