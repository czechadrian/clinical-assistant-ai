"use client";

/**
 * /admin/docs — Admin-only document upload page.
 *
 * Flow:
 *   1. Verify user is admin (profiles.role = 'admin').
 *   2. Pick a file; compute SHA-256 in-browser.
 *   3. Upload file to Supabase Storage bucket: medical_docs.
 *   4. POST metadata to /docs API (FastAPI validates, PostgREST enforces RLS).
 *
 * Storage and metadata RLS are enforced server-side — even if this page is
 * reached by a non-admin, all writes will be rejected by the database.
 *
 * No secrets are used on the client side; only the anon key is present.
 */

import { useEffect, useRef, useState } from "react";
import { supabase } from "@/lib/supabaseClient";
import { apiFetch, listDocs, type DocOut } from "@/lib/api";

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

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export default function AdminDocsPage() {
  const [admin, setAdmin] = useState<boolean | null>(null); // null = loading
  const [docs, setDocs] = useState<DocOut[]>([]);
  const [uploading, setUploading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState<string | null>(null);
  const fileRef = useRef<HTMLInputElement>(null);
  const titleRef = useRef<HTMLInputElement>(null);

  // Check admin + load docs on mount
  useEffect(() => {
    let cancelled = false;
    isAdmin().then((ok) => {
      if (cancelled) return;
      setAdmin(ok);
      if (ok) {
        listDocs()
          .then((d) => { if (!cancelled) setDocs(d); })
          .catch(() => { /* non-critical: docs list failed */ });
      }
    });
    return () => { cancelled = true; };
  }, []);

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

  // ── Admin view ───────────────────────────────────────────────────────────
  return (
    <main className="mx-auto max-w-2xl px-6 py-12">
      <h1 className="mb-6 text-lg font-semibold text-zinc-900 dark:text-zinc-50">
        Upload medical document
      </h1>

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

        {error && <p className="text-xs text-red-500">{error}</p>}
        {success && <p className="text-xs text-emerald-600">{success}</p>}

        <button
          type="submit"
          disabled={uploading}
          className="rounded-lg bg-zinc-900 px-4 py-2 text-sm font-medium text-zinc-50 transition hover:bg-zinc-700 disabled:opacity-50 dark:bg-zinc-50 dark:text-zinc-950 dark:hover:bg-zinc-200"
        >
          {uploading ? "Uploading…" : "Upload"}
        </button>
      </form>

      {/* Existing docs */}
      {docs.length > 0 && (
        <section className="mt-10">
          <h2 className="mb-3 text-sm font-medium text-zinc-500">
            Uploaded documents ({docs.length})
          </h2>
          <ul className="space-y-2">
            {docs.map((doc) => (
              <li
                key={doc.id}
                className="flex items-center justify-between rounded-lg border border-zinc-200 px-3 py-2 text-xs dark:border-zinc-700"
              >
                <span className="font-medium text-zinc-800 dark:text-zinc-200">
                  {doc.title}
                </span>
                <span
                  className={`rounded-full px-2 py-0.5 font-medium ${
                    doc.status === "indexed"
                      ? "bg-emerald-100 text-emerald-700"
                      : doc.status === "failed"
                        ? "bg-red-100 text-red-700"
                        : "bg-amber-100 text-amber-700"
                  }`}
                >
                  {doc.status}
                </span>
              </li>
            ))}
          </ul>
        </section>
      )}
    </main>
  );
}
