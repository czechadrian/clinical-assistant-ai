# Tool modules for the Kliniczny Asystent AI chat pipeline.
#
# Tools are single-purpose, schema-validated retrieval/computation units.
# Constraints (enforced by design, not trust):
#   - No external HTTP calls — only internal Supabase RPC via injected db_rpc.
#   - RLS is preserved: every tool receives and forwards the user's JWT.
#   - Dependencies (embedder, db_rpc) are injected — no module-level singletons.
#   - Schemas (Pydantic) define the contract; callers validate at the boundary.
