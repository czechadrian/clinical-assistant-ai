"use client";

import { useState } from "react";
import { supabase } from "@/lib/supabaseClient";

type WhoAmIResponse = {
  user_id: string;
  email: string | null;
};

const backendBaseUrl =
  process.env.NEXT_PUBLIC_BACKEND_URL ?? "http://localhost:8000";

export function TestApiButton() {
  const [loading, setLoading] = useState(false);
  const [data, setData] = useState<WhoAmIResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function handleClick() {
    setLoading(true);
    setError(null);
    setData(null);

    const {
      data: { session },
      error: sessionError,
    } = await supabase.auth.getSession();

    if (sessionError || !session?.access_token) {
      setLoading(false);
      setError("No Supabase session/access token");
      return;
    }

    const token = session.access_token;

    try {
      const res = await fetch(`${backendBaseUrl}/whoami`, {
        method: "GET",
        headers: {
          Authorization: `Bearer ${token}`,
        },
      });

      const body = await res.json().catch(() => null);

      if (!res.ok) {
        throw new Error(
          (body && (body.detail || body.error)) ||
            `Request failed with status ${res.status}`,
        );
      }

      setData(body as WhoAmIResponse);
    } catch (err: unknown) {
      const message =
        err instanceof Error ? err.message : "Unknown error during API call";
      setError(message);
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="space-y-3">
      <button
        type="button"
        onClick={handleClick}
        disabled={loading}
        className="rounded-md border border-zinc-300 px-3 py-1.5 text-xs font-medium text-zinc-800 transition hover:bg-zinc-100 disabled:opacity-60 dark:border-zinc-700 dark:text-zinc-100 dark:hover:bg-zinc-800"
      >
        {loading ? "Testing..." : "Test API"}
      </button>

      {error && (
        <pre className="whitespace-pre-wrap rounded-md bg-red-50 p-2 text-xs text-red-800 dark:bg-red-950/40 dark:text-red-100">
Error: {error}
        </pre>
      )}

      {data && (
        <pre className="whitespace-pre-wrap rounded-md bg-zinc-100 p-2 text-xs text-zinc-900 dark:bg-zinc-900 dark:text-zinc-100">
{JSON.stringify(data, null, 2)}
        </pre>
      )}
    </div>
  );
}

