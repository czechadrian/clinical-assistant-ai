"use client";

import { useEffect, useRef, useState } from "react";
import { supabase } from "@/lib/supabaseClient";
import { postChat } from "@/lib/api";

type Message = {
  id: string;
  role: string;
  content: string;
};

type Props = {
  conversationId: string | null;
  onConversationCreated: (id: string) => void;
};

export function MessagePanel({ conversationId, onConversationCreated }: Props) {
  const [messages, setMessages] = useState<Message[]>([]);
  const [input, setInput] = useState("");
  const [sending, setSending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const bottomRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  // Fetch messages whenever the selected conversation changes
  useEffect(() => {
    if (!conversationId) {
      setMessages([]);
      return;
    }

    let cancelled = false;

    supabase
      .from("messages")
      .select("id, role, content")
      .eq("conversation_id", conversationId)
      .order("created_at", { ascending: true })
      .then(({ data, error }) => {
        if (cancelled) return;
        if (error) setError(error.message);
        else setMessages(data ?? []);
      });

    return () => {
      cancelled = true;
    };
  }, [conversationId]);

  // Keep the latest message in view
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  async function handleSend() {
    const text = input.trim();
    if (!text || sending) return;

    setSending(true);
    setError(null);
    setInput("");

    try {
      const result = await postChat(text, conversationId ?? undefined);

      // Append both messages from the API response — no extra DB round-trip.
      // If this was a new conversation, the parent will also trigger a
      // conversation-list refresh, which will cause a re-fetch of messages
      // via the effect above. Content is identical so no visible flash.
      setMessages((prev) => [...prev, result.user_message, result.assistant_message]);

      if (!conversationId) {
        onConversationCreated(result.conversation_id);
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "Send failed");
      setInput(text); // restore on error so the user doesn't lose the message
    } finally {
      setSending(false);
      inputRef.current?.focus();
    }
  }

  return (
    <div className="flex flex-1 flex-col overflow-hidden">
      {/* Messages area */}
      <div className="flex-1 overflow-y-auto px-6 py-4">
        {messages.length === 0 ? (
          <p className="mt-8 text-center text-sm text-zinc-400">
            {conversationId ? "No messages yet." : "Send a message to start a conversation."}
          </p>
        ) : (
          <div className="space-y-3">
            {messages.map((m) => (
              <div
                key={m.id}
                className={`flex ${m.role === "user" ? "justify-end" : "justify-start"}`}
              >
                <div
                  className={`max-w-[70%] rounded-2xl px-4 py-2 text-sm leading-relaxed ${
                    m.role === "user"
                      ? "bg-zinc-900 text-zinc-50 dark:bg-zinc-50 dark:text-zinc-950"
                      : "bg-zinc-100 text-zinc-900 dark:bg-zinc-800 dark:text-zinc-50"
                  }`}
                >
                  {m.content}
                </div>
              </div>
            ))}
            <div ref={bottomRef} />
          </div>
        )}
      </div>

      {/* Input bar — always visible; sending with no conversation creates one */}
      <div className="border-t border-zinc-200 bg-white px-4 py-3 dark:border-zinc-800 dark:bg-zinc-950">
        {error && <p className="mb-2 text-xs text-red-500">{error}</p>}
        <form
          onSubmit={(e) => {
            e.preventDefault();
            void handleSend();
          }}
          className="flex gap-2"
        >
          <input
            ref={inputRef}
            value={input}
            onChange={(e) => setInput(e.target.value)}
            disabled={sending}
            placeholder="Type a message…"
            className="flex-1 rounded-lg border border-zinc-200 bg-zinc-50 px-3 py-2 text-sm text-zinc-900 outline-none transition focus:border-zinc-400 disabled:opacity-50 dark:border-zinc-700 dark:bg-zinc-900 dark:text-zinc-50 dark:focus:border-zinc-500"
          />
          <button
            type="submit"
            disabled={sending || !input.trim()}
            className="rounded-lg bg-zinc-900 px-4 py-2 text-sm font-medium text-zinc-50 transition hover:bg-zinc-700 disabled:cursor-not-allowed disabled:opacity-50 dark:bg-zinc-50 dark:text-zinc-950 dark:hover:bg-zinc-200"
          >
            {sending ? "…" : "Send"}
          </button>
        </form>
      </div>
    </div>
  );
}
