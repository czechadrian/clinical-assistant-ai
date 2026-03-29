# Vercel Deployment Guide — Kliniczny Asystent AI (`/web`)

## Monorepo setup

| Setting | Value |
|---|---|
| **Root directory** | `web` |
| **Framework preset** | Next.js |
| **Node version** | 20.x (LTS) |
| **Build command** | `pnpm build` (auto-detected) |
| **Install command** | `pnpm install` |

Set **Root directory → `web`** in Vercel project settings. Vercel will treat `/web` as the project root, so `package.json`, `pnpm-lock.yaml`, and `.env.local` are resolved relative to it.

---

## Environment variables

### Rules

1. **Never** put secrets (`SUPABASE_SERVICE_ROLE_KEY`, `ANTHROPIC_API_KEY`) in any `NEXT_PUBLIC_*` variable — they would be embedded in the browser bundle.
2. `NEXT_PUBLIC_*` variables are safe **only** for values users can see: the Supabase project URL, the anon key, the API base URL.
3. All server-only secrets (if any) live in Vercel's encrypted env vars and are only accessible in API routes / Server Components, never in client components.

### Variable reference

| Variable | Scope | Description |
|---|---|---|
| `NEXT_PUBLIC_SUPABASE_URL` | All | Supabase project URL (safe for browser) |
| `NEXT_PUBLIC_SUPABASE_ANON_KEY` | All | Supabase anon/public key (safe for browser) |
| `NEXT_PUBLIC_API_BASE_URL` | All | FastAPI backend URL, e.g. `https://api.yourdomain.com` |
| `NEXT_PUBLIC_APP_VERSION` | All | Optional: git tag or semver, e.g. `1.2.0` |
| `NEXT_PUBLIC_STREAMING_ENABLED` | All | `"true"` to enable `/chat/stream` UI; `"false"` otherwise |

### Per-environment values in Vercel

In **Project → Settings → Environment Variables**, set each variable for the correct scope:

| Variable | Production | Preview | Development |
|---|---|---|---|
| `NEXT_PUBLIC_SUPABASE_URL` | prod project URL | staging project URL | `http://localhost:54321` |
| `NEXT_PUBLIC_SUPABASE_ANON_KEY` | prod anon key | staging anon key | local anon key |
| `NEXT_PUBLIC_API_BASE_URL` | `https://api.yourdomain.com` | preview API URL | `http://localhost:8000` |
| `NEXT_PUBLIC_APP_VERSION` | Git tag auto-populated via CI | `preview` | `dev` |

---

## Supabase Auth redirect URLs

Supabase enforces an allowlist for OAuth/email-link redirects. Add these in:
**Supabase Dashboard → Authentication → URL Configuration**

### Required entries

```
# Production
https://yourdomain.com/auth/callback
https://yourdomain.com

# Vercel Preview (wildcard — covers all PR preview deployments)
https://*.vercel.app/auth/callback
https://*.vercel.app

# Local development
http://localhost:3000/auth/callback
http://localhost:3000
```

> **Important:** Without the `*.vercel.app` wildcard, PR preview deployments will fail on login redirect. Supabase supports `*` wildcard for subdomains.

---

## Deploy checklist (run after every production deploy)

- [ ] `GET /health` on the API returns `{"status": "ok"}`
- [ ] Login flow completes end-to-end (Supabase Auth redirect works)
- [ ] `/chat` returns a valid assistant payload (status 200)
- [ ] PII input (email address) returns 400 with `error.code == "PII_DETECTED"`
- [ ] No `SUPABASE_SERVICE_ROLE_KEY` or `ANTHROPIC_API_KEY` appear in browser Network tab responses
- [ ] Browser console shows no `NEXT_PUBLIC_*` warnings about missing variables
- [ ] `/status` page shows correct `APP_VERSION` and `API_BASE_URL`

---

## Smoke test — quick CLI check against production

```bash
# Substitute your production API URL
BASE=https://api.yourdomain.com

# Health
curl -sf "$BASE/health" | jq .

# PII guard (expect 400 + PII_DETECTED)
curl -sf -X POST "$BASE/chat" \
  -H "Authorization: Bearer <your_token>" \
  -H "Content-Type: application/json" \
  -d '{"mode":"triage","input_text":"contact jan@example.com","conversation_id":"<id>"}' \
  | jq '.error.code'
```

---

## Common pitfalls

### 401 on every request after deploy

**Cause:** `NEXT_PUBLIC_API_BASE_URL` points to wrong environment (e.g. localhost in prod build).
**Fix:** Check Vercel env vars → Production scope has the real API URL.

### Login redirects to wrong URL / stays on Supabase

**Cause:** The deployed Vercel URL is not in Supabase's redirect allowlist.
**Fix:** Add `https://*.vercel.app/**` to **Supabase → Auth → URL Configuration → Redirect URLs**.

### Preview deployments use production Supabase data

**Cause:** `NEXT_PUBLIC_SUPABASE_URL` has the same value in Preview and Production scopes.
**Fix:** Create a separate Supabase project for staging; set different keys in Preview scope.

### Build fails with "Missing Supabase environment variables"

**Cause:** `supabaseClient.ts` throws at import time if env vars are absent.
**Fix:** Ensure `NEXT_PUBLIC_SUPABASE_URL` and `NEXT_PUBLIC_SUPABASE_ANON_KEY` are set in the Vercel scope that is building.
