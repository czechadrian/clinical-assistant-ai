"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { supabase } from "@/lib/supabaseClient";
import { listConversations } from "@/lib/api";
import { ConversationList, type Conversation } from "@/components/ConversationList";
import { MessagePanel } from "@/components/MessagePanel";

type PageState =
  | { status: "loading" }
  | { status: "error"; message: string }
  | { status: "ready"; email: string | null };

export default function AppPage() {
  const router = useRouter();
  const [pageState, setPageState] = useState<PageState>({ status: "loading" });
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);

  // Fetch conversations via the backend so RLS is enforced through the API layer.
  async function fetchConversations() {
    const data = await listConversations().catch(() => []);
    setConversations(data);
  }

  useEffect(() => {
    let cancelled = false;

    async function init() {
      // getUser() validates server-side — safe source of user_id for DB writes
      const {
        data: { user },
        error,
      } = await supabase.auth.getUser();

      if (cancelled) return;
      if (error || !user) { router.replace("/login"); return; }

      // Ensure the profile row exists (idempotent)
      const { error: upsertError } = await supabase
        .from("profiles")
        .upsert({ id: user.id, email: user.email ?? null }, { onConflict: "id" });

      if (cancelled) return;
      if (upsertError) { setPageState({ status: "error", message: upsertError.message }); return; }

      setPageState({ status: "ready", email: user.email ?? null });
      await fetchConversations();
    }

    void init();
    return () => { cancelled = true; };
  }, [router]);

  async function handleLogout() {
    await supabase.auth.signOut();
    router.replace("/login");
  }

  // Called by MessagePanel when a message creates a new conversation
  async function handleConversationCreated(id: string) {
    await fetchConversations();
    setSelectedId(id);
  }

  if (pageState.status === "loading") {
    return (
      <div className="flex min-h-screen items-center justify-center bg-zinc-50 font-sans dark:bg-black">
        <div className="rounded-full border border-zinc-200 bg-white px-4 py-2 text-sm text-zinc-600 shadow-sm dark:border-zinc-800 dark:bg-zinc-950 dark:text-zinc-300">
          Loading…
        </div>
      </div>
    );
  }

  if (pageState.status === "error") {
    return (
      <div className="flex min-h-screen items-center justify-center bg-zinc-50 font-sans dark:bg-black">
        <div className="w-full max-w-md rounded-2xl border border-red-200 bg-red-50 p-6 text-sm text-red-900 dark:border-red-900/60 dark:bg-red-950/40 dark:text-red-100">
          <p className="font-semibold">Something went wrong</p>
          <p className="mt-2">{pageState.message}</p>
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
    <div className="flex h-screen overflow-hidden bg-zinc-50 font-sans dark:bg-black">
      <ConversationList
        conversations={conversations}
        selectedId={selectedId}
        onSelect={setSelectedId}
        onNew={() => setSelectedId(null)}
        email={pageState.email}
        onLogout={handleLogout}
      />
      <MessagePanel
        conversationId={selectedId}
        onConversationCreated={handleConversationCreated}
      />
    </div>
  );
}
