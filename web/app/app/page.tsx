"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { supabase } from "@/lib/supabaseClient";
import { TestApiButton } from "@/components/TestApiButton";

type AppState =
  | { status: "loading" }
  | { status: "error"; message: string }
  | { status: "ready"; email: string | null };

export default function AppPage() {
  const router = useRouter();
  const [state, setState] = useState<AppState>({ status: "loading" });

  useEffect(() => {
    let cancelled = false;

    async function init() {
      // getUser() validates the token server-side — safe to use as the
      // source of truth for user.id before writing to the database.
      const {
        data: { user },
        error,
      } = await supabase.auth.getUser();

      if (cancelled) return;

      if (error || !user) {
        router.replace("/login");
        return;
      }

      const { error: upsertError } = await supabase
        .from("profiles")
        .upsert({ id: user.id, email: user.email ?? null }, { onConflict: "id" });

      if (cancelled) return;

      if (upsertError) {
        setState({ status: "error", message: upsertError.message });
        return;
      }

      setState({ status: "ready", email: user.email ?? null });
    }

    void init();

    return () => {
      cancelled = true;
    };
  }, [router]);

  async function handleLogout() {
    await supabase.auth.signOut();
    router.replace("/login");
  }

  if (state.status === "loading") {
    return (
      <div className="flex min-h-screen items-center justify-center bg-zinc-50 font-sans dark:bg-black">
        <div className="rounded-full border border-zinc-200 bg-white px-4 py-2 text-sm text-zinc-600 shadow-sm dark:border-zinc-800 dark:bg-zinc-950 dark:text-zinc-300">
          Loading your profile…
        </div>
      </div>
    );
  }

  if (state.status === "error") {
    return (
      <div className="flex min-h-screen items-center justify-center bg-zinc-50 font-sans dark:bg-black">
        <div className="w-full max-w-md rounded-2xl border border-red-200 bg-red-50 p-6 text-sm text-red-900 dark:border-red-900/60 dark:bg-red-950/40 dark:text-red-100">
          <p className="font-semibold">Something went wrong</p>
          <p className="mt-2">{state.message}</p>
          <button
            onClick={handleLogout}
            className="mt-4 rounded-lg border border-red-300 px-3 py-1.5 text-xs font-medium text-red-900 transition hover:bg-red-100 dark:border-red-700 dark:text-red-100 dark:hover:bg-red-900/40"
          >
            Log out
          </button>
        </div>
      </div>
    );
  }

  return (
    <div className="flex min-h-screen items-center justify-center bg-zinc-50 font-sans dark:bg-black">
      <main className="w-full max-w-2xl rounded-2xl bg-white p-10 shadow-sm dark:bg-zinc-950">
        <div className="mb-4 flex items-center justify-between gap-4">
          <h1 className="text-3xl font-semibold tracking-tight text-zinc-900 dark:text-zinc-50">
            App
          </h1>
          <button
            onClick={handleLogout}
            className="rounded-lg border border-zinc-200 px-3 py-1.5 text-xs font-medium text-zinc-700 transition hover:bg-zinc-100 dark:border-zinc-700 dark:text-zinc-200 dark:hover:bg-zinc-800"
          >
            Log out
          </button>
        </div>

        <p className="mb-2 text-sm text-zinc-600 dark:text-zinc-400">
          You are logged in via Supabase.
        </p>
        {state.email ? (
          <p className="mb-4 text-sm text-zinc-800 dark:text-zinc-200">
            Signed in as <span className="font-medium">{state.email}</span>
          </p>
        ) : (
          <p className="mb-4 text-sm text-zinc-800 dark:text-zinc-200">
            Signed in user has no email.
          </p>
        )}

        <section className="mt-6 border-t border-zinc-200 pt-4 dark:border-zinc-800">
          <h2 className="mb-2 text-sm font-semibold text-zinc-900 dark:text-zinc-50">
            Test backend API
          </h2>
          <TestApiButton />
        </section>
      </main>
    </div>
  );
}
