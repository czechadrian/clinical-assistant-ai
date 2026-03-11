Web: cd web && pnpm dev
API: cd api && uv run uvicorn main:app --reload --port 8000
Ingest: cd ingest && uv run python worker.py
