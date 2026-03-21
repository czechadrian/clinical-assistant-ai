"use client";

import { useEffect, useState } from "react";

// Reads the same env var used by lib/api.ts so there is one source of truth
// for the backend URL. NEXT_PUBLIC_* is inlined at build time — safe to use
// in the browser because it's just a URL, not a secret.
const API_BASE = process.env.NEXT_PUBLIC_BACKEND_URL ?? "http://localhost:8000";

type Status = "loading" | "ok" | "error";

export function ApiStatus() {
  const [status, setStatus] = useState<Status>("loading");

  useEffect(() => {
    let cancelled = false;

    fetch(`${API_BASE}/health`)
      .then((res) => {
        if (cancelled) return;
        // Treat any non-2xx as an error even if the network succeeded
        setStatus(res.ok ? "ok" : "error");
      })
      .catch(() => {
        // Network failure, CORS block, or server down
        if (!cancelled) setStatus("error");
      });

    return () => {
      cancelled = true;
    };
  }, []);

  const dot =
    status === "loading"
      ? "animate-pulse bg-zinc-300 dark:bg-zinc-600"
      : status === "ok"
        ? "bg-emerald-500"
        : "bg-red-500";

  const label =
    status === "loading" ? "Checking API…" :
    status === "ok"      ? "API online" :
                           "API offline";

  return (
    <div className="flex items-center gap-2 px-1 py-1">
      <span className={`h-2 w-2 shrink-0 rounded-full ${dot}`} />
      <span className="text-xs text-zinc-400 dark:text-zinc-500">{label}</span>
    </div>
  );
}
