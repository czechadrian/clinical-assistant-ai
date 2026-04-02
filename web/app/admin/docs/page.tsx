"use client";

/**
 * /admin/docs — Admin-only document upload + ingest management page.
 *
 * Flow:
 *   1. Verify user is admin (profiles.role = 'admin').
 *   2. Pick a file; compute SHA-256 in-browser.
 *   3. Upload file to Supabase Storage bucket: medical_docs.
 *   4. POST metadata to /docs API (FastAPI validates, PostgREST enforces RLS).
 *   5. Admin can trigger re-ingest via POST /admin/ingest.
 *   6. Page auto-refreshes doc list every 5 s while any doc is 'processing'.
 *
 * Storage and metadata RLS are enforced server-side — even if this page is
 * reached by a non-admin, all writes will be rejected by the database.
 *
 * No secrets are used on the client side; only the anon key is present.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { supabase } from "@/lib/supabaseClient";
import {
  apiFetch,
  listDocs,
  triggerIngest,
  resetStuckDocs,
  type DocOut,
  type IngestTriggerResponse,
} from "@/lib/api";

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

async function sha256Hex(buffer: ArrayBuffer): Promise<string> {
  const hashBuffer = await crypto.subtle.digest("SHA-256", buffer);
  return Array.from(new Uint8Array(hashBuffer))
    .map((b) => b.toString(16).padStart(2, "0"))
    .join("");
}

async function isAdmin(): Promise<boolean> {
  const {
    data: { user },
  } = await supabase.auth.getUser();
  return user?.app_metadata?.role === "admin" || false;
}

function statusBadge(status: DocOut["status"]): React.ReactNode {
  const styles: Record<string, string> = {
    ready:      "bg-emerald-100 text-emerald-700 dark:bg-emerald-900/40 dark:text-emerald-300",
    indexed:    "bg-emerald-100 text-emerald-700 dark:bg-emerald-900/40 dark:text-emerald-300",
    processing: "bg-blue-100 text-blue-700 dark:bg-blue-900/40 dark:text-blue-300",
    pending:    "bg-amber-100 text-amber-700 dark:bg-amber-900/40 dark:text-amber-300",
    failed:     "bg-red-100 text-red-700 dark:bg-red-900/40 dark:text-red-300",
  };
  const cls = styles[status] ?? "bg-zinc-100 text-zinc-600";
  return (
    <span className={`rounded-full px-2 py-0.5 font-medium ${cls}`}>
      {status}
    </span>
  );
}

function formatDate(iso: string | null): string {
  if (!iso) return "—";
  return new Date(iso).toLocaleString();
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export default function AdminDocsPage() {
  const [admin, setAdmin] = useState<boolean | null>(null); // null = loading
  const [docs, setDocs] = useState<DocOut[]>([]);
  const [uploading, setUploading] = useState(false);
  const [triggering, setTriggering] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState<string | null>(null);
  const fileRef = useRef<HTMLInputElement>(null);
  const titleRef = useRef<HTMLInputElement>(null);

  const refreshDocs = useCallback(async () => {
    const d = await listDocs().catch(() => null);
    if (d) setDocs(d);
  }, []);

  // Check admin + load docs on mount
  useEffect(() => {
    let cancelled = false;
    isAdmin().then((ok) => {
      if (cancelled) return;
      setAdmin(ok);
      if (ok) void refreshDocs();
    });
    return () => { cancelled = true; };
  }, [refreshDocs]);

  // Auto-poll every 5 s while any doc is in 'processing' state
  useEffect(() => {
    if (!admin) return;
    const anyProcessing = docs.some((d) => d.status === "processing");
    if (!anyProcessing) return;

    const interval = setInterval(() => { void refreshDocs(); }, 5000);
    return () => clearInterval(interval);
  }, [admin, docs, refreshDocs]);

  async function handleUpload(e: React.FormEvent) {
    e.preventDefault();
    const file = fileRef.current?.files?.[0];
    const title = titleRef.current?.value.trim();
    if (!file || !title) return;

    setUploading(true);
    setError(null);
    setSuccess(null);

    try {
      // 1. Compute SHA-256 hash of file content
      const arrayBuffer = await file.arrayBuffer();
      const hash = await sha256Hex(arrayBuffer);

      // 2. Upload to Supabase Storage (storage RLS enforces admin role)
      const storagePath = `${Date.now()}-${file.name}`;
      const { error: storageError } = await supabase.storage
        .from("medical_docs")
        .upload(storagePath, file, { contentType: file.type, upsert: false });

      if (storageError) {
        throw new Error(`Storage upload failed: ${storageError.message}`);
      }

      // 3. Create metadata record via FastAPI (PostgREST RLS enforces admin role)
      const doc = await apiFetch<DocOut>("/docs", {
        method: "POST",
        body: JSON.stringify({
          title,
          filename: file.name,
          storage_path: storagePath,
          file_hash: hash,
          version: "1",
        }),
      });

      setDocs((prev) => [doc, ...prev]);
      setSuccess(`Uploaded: ${doc.title} (${doc.id.slice(0, 8)}…)`);

      // Reset form
      if (fileRef.current) fileRef.current.value = "";
      if (titleRef.current) titleRef.current.value = "";
    } catch (err) {
      setError(err instanceof Error ? err.message : "Upload failed");
    } finally {
      setUploading(false);
    }
  }

  async function handleTriggerIngest() {
    setTriggering(true);
    setError(null);
    setSuccess(null);
    try {
      const result: IngestTriggerResponse = await triggerIngest();
      setSuccess(result.message);
      await refreshDocs();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to trigger ingest");
    } finally {
      setTriggering(false);
    }
  }

  async function handleResetStuck() {
    setError(null);
    setSuccess(null);
    try {
      const result: IngestTriggerResponse = await resetStuckDocs();
      setSuccess(result.message);
      await refreshDocs();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to reset stuck docs");
    }
  }

  // ── Loading ──────────────────────────────────────────────────────────────
  if (admin === null) {
    return (
      <main className="px-6 py-12 text-sm text-zinc-400">Checking access…</main>
    );
  }

  // ── Access denied ────────────────────────────────────────────────────────
  if (!admin) {
    return (
      <main className="px-6 py-12">
        <p className="text-sm text-red-500">Access denied. Admin role required.</p>
      </main>
    );
  }

  const anyProcessing = docs.some((d) => d.status === "processing");
  const anyStuck = docs.some((d) => d.status === "processing");
  const pendingCount = docs.filter((d) => d.status === "pending").length;

  // ── Admin view ───────────────────────────────────────────────────────────
  return (
    <main className="mx-auto max-w-2xl px-6 py-12">
      <h1 className="mb-6 text-lg font-semibold text-zinc-900 dark:text-zinc-50">
        Document management
      </h1>

      {/* ── Upload form ── */}
      <section className="mb-8">
        <h2 className="mb-3 text-sm font-medium text-zinc-500">Upload document</h2>
        <form onSubmit={(e) => void handleUpload(e)} className="space-y-4">
          <div>
            <label className="mb-1 block text-xs font-medium text-zinc-500">
              Document title
            </label>
            <input
              ref={titleRef}
              type="text"
              required
              maxLength={200}
              placeholder="e.g. PTK Guidelines 2024 — Cardiology"
              className="w-full rounded-lg border border-zinc-200 bg-zinc-50 px-3 py-2 text-sm outline-none focus:border-zinc-400 dark:border-zinc-700 dark:bg-zinc-900 dark:text-zinc-50"
            />
          </div>

          <div>
            <label className="mb-1 block text-xs font-medium text-zinc-500">
              File (PDF recommended)
            </label>
            <input
              ref={fileRef}
              type="file"
              required
              accept=".pdf,.txt,.md"
              className="w-full text-sm text-zinc-700 dark:text-zinc-300"
            />
          </div>

          <button
            type="submit"
            disabled={uploading}
            className="rounded-lg bg-zinc-900 px-4 py-2 text-sm font-medium text-zinc-50 transition hover:bg-zinc-700 disabled:opacity-50 dark:bg-zinc-50 dark:text-zinc-950 dark:hover:bg-zinc-200"
          >
            {uploading ? "Uploading…" : "Upload"}
          </button>
        </form>
      </section>

      {/* ── Ingest controls ── */}
      <section className="mb-8 flex flex-wrap items-center gap-3">
        <button
          onClick={() => void handleTriggerIngest()}
          disabled={triggering || anyProcessing || pendingCount === 0}
          className="rounded-lg border border-zinc-300 px-4 py-2 text-sm font-medium text-zinc-800 transition hover:bg-zinc-100 disabled:opacity-50 dark:border-zinc-600 dark:text-zinc-200 dark:hover:bg-zinc-800"
          title={
            anyProcessing
              ? "Ingest already running"
              : pendingCount === 0
              ? "No pending documents"
              : `Process ${pendingCount} pending document(s)`
          }
        >
          {triggering ? "Triggering…" : anyProcessing ? "Ingesting…" : `Run ingest (${pendingCount} pending)`}
        </button>

        {anyStuck && (
          <button
            onClick={() => void handleResetStuck()}
            className="rounded-lg border border-amber-300 px-4 py-2 text-sm font-medium text-amber-800 transition hover:bg-amber-50 dark:border-amber-600 dark:text-amber-300 dark:hover:bg-amber-900/30"
            title="Reset documents stuck in processing state back to pending"
          >
            Reset stuck
          </button>
        )}

        {anyProcessing && (
          <span className="text-xs text-blue-600 dark:text-blue-400">
            Refreshing every 5 s…
          </span>
        )}
      </section>

      {error && <p className="mb-4 text-xs text-red-500">{error}</p>}
      {success && <p className="mb-4 text-xs text-emerald-600">{success}</p>}

      {/* ── Document list ── */}
      {docs.length > 0 && (
        <section>
          <h2 className="mb-3 text-sm font-medium text-zinc-500">
            Documents ({docs.length})
          </h2>
          <ul className="space-y-2">
            {docs.map((doc) => (
              <li
                key={doc.id}
                className="rounded-lg border border-zinc-200 px-3 py-3 text-xs dark:border-zinc-700"
              >
                <div className="flex items-center justify-between gap-2">
                  <span className="font-medium text-zinc-800 dark:text-zinc-200">
                    {doc.title}
                  </span>
                  {statusBadge(doc.status)}
                </div>

                <div className="mt-1.5 space-y-0.5 text-zinc-400">
                  <div>
                    <span className="font-medium">File:</span> {doc.filename} · v{doc.version}
                  </div>
                  {doc.last_ingested_at && (
                    <div>
                      <span className="font-medium">Last indexed:</span>{" "}
                      {formatDate(doc.last_ingested_at)}
                    </div>
                  )}
                  {doc.last_error_code && doc.status === "failed" && (
                    <div className="text-red-400">
                      <span className="font-medium">Error:</span> {doc.last_error_code}
                    </div>
                  )}
                </div>
              </li>
            ))}
          </ul>
        </section>
      )}
    </main>
  );
}
