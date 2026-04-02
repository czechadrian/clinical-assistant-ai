# Kliniczny Asystent AI — Frontend

This is the Next.js 16 App Router frontend for the clinical assistant.

Full documentation, architecture overview, and setup instructions are in the [root README](../README.md).

## Quick start

```bash
pnpm install
cp .env.local.example .env.local   # fill in Supabase + API URL
pnpm dev                           # http://localhost:3000
```

## Commands

```bash
pnpm dev                      # development server
pnpm build                    # production build
pnpm lint                     # ESLint
pnpm exec tsc --noEmit        # type-check (run before any TS change is considered done)
```
