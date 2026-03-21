"use client";

import { useEffect, useRef, useState } from "react";
import { supabase } from "@/lib/supabaseClient";
import { postChat, type AssistantPayload, type ChatMode } from "@/lib/api";

type Message = {
  id: string;
  role: string;
  content: string; // JSON-encoded AssistantPayload for assistant messages
};

const FLAG_STYLES: Record<AssistantPayload["flag"], string> = {
  safe: "bg-emerald-100 text-emerald-700 dark:bg-emerald-900/40 dark:text-emerald-400",
  uncertain: "bg-amber-100 text-amber-700 dark:bg-amber-900/40 dark:text-amber-400",
  refuse: "bg-red-100 text-red-700 dark:bg-red-900/40 dark:text-red-400",
};

const FLAG_LABELS: Record<AssistantPayload["flag"], string> = {
  safe: "bezpieczne",
  uncertain: "wymaga weryfikacji",
  refuse: "poza zakresem",
};

function AssistantBubble({ content }: { content: string }) {
  let payload: AssistantPayload | null = null;
  try {
    payload = JSON.parse(content) as AssistantPayload;
  } catch {
    // Legacy or malformed content — render as plain text
  }

  if (!payload) {
    return <span>{content}</span>;
  }

  return (
    <div className="space-y-3 text-sm">
      {/* Flag badge */}
      <span
        className={`inline-block rounded-full px-2 py-0.5 text-xs font-medium ${FLAG_STYLES[payload.flag]}`}
      >
        {FLAG_LABELS[payload.flag]}
      </span>

      {/* Red flags — shown first and highlighted */}
      {payload.red_flags.length > 0 && (
        <div className="rounded-lg border border-red-200 bg-red-50 px-3 py-2 dark:border-red-900/50 dark:bg-red-950/30">
          <p className="mb-1 text-xs font-semibold uppercase tracking-wide text-red-600 dark:text-red-400">
            Red flags
          </p>
          <ul className="space-y-1">
            {payload.red_flags.map((f, i) => (
              <li key={i} className="flex gap-2 text-xs text-red-700 dark:text-red-300">
                <span className="mt-0.5 shrink-0">▲</span>
                {f}
              </li>
            ))}
          </ul>
        </div>
      )}

      {/* Clarifying questions */}
      {payload.questions_to_ask.length > 0 && (
        <div>
          <p className="mb-1 text-xs font-semibold uppercase tracking-wide text-zinc-500 dark:text-zinc-400">
            Questions to ask
          </p>
          <ul className="space-y-1">
            {payload.questions_to_ask.map((q, i) => (
              <li key={i} className="flex gap-2 text-xs leading-relaxed text-zinc-700 dark:text-zinc-300">
                <span className="mt-0.5 shrink-0 text-zinc-400">?</span>
                {q}
              </li>
            ))}
          </ul>
        </div>
      )}

      {/* Next steps */}
      {payload.possible_next_steps.length > 0 && (
        <div>
          <p className="mb-1 text-xs font-semibold uppercase tracking-wide text-zinc-500 dark:text-zinc-400">
            Possible next steps
          </p>
          <ol className="space-y-1">
            {payload.possible_next_steps.map((s, i) => (
              <li key={i} className="flex gap-2 text-xs leading-relaxed text-zinc-700 dark:text-zinc-300">
                <span className="mt-0.5 shrink-0 font-medium text-zinc-400">{i + 1}.</span>
                {s}
              </li>
            ))}
          </ol>
        </div>
      )}

      {/* Patient-facing summary */}
      <div className="rounded-lg bg-zinc-50 px-3 py-2 dark:bg-zinc-900/50">
        <p className="mb-1 text-xs font-semibold uppercase tracking-wide text-zinc-500 dark:text-zinc-400">
          Patient summary
        </p>
        <p className="text-xs leading-relaxed text-zinc-600 dark:text-zinc-300">
          {payload.patient_facing_summary}
        </p>
      </div>

      {/* Sources */}
      {payload.sources.length > 0 && (
        <div>
          <p className="mb-1 text-xs font-semibold uppercase tracking-wide text-zinc-500 dark:text-zinc-400">
            Sources
          </p>
          <ul className="space-y-0.5">
            {payload.sources.map((src) => (
              <li key={src.id} className="text-xs text-zinc-500 dark:text-zinc-400">
                <span className="font-medium">{src.id}</span> — {src.title}
                {src.section && (
                  <span className="text-zinc-400"> · {src.section}</span>
                )}
              </li>
            ))}
          </ul>
        </div>
      )}

      {/* Disclaimer */}
      <p className="border-t border-zinc-200 pt-2 text-xs text-zinc-400 dark:border-zinc-700 dark:text-zinc-500">
        {payload.disclaimer}
      </p>
    </div>
  );
}

type Props = {
  conversationId: string | null;
  onConversationCreated: (id: string) => void;
};

const MODE_LABELS: Record<ChatMode, string> = {
  triage: "Triage",
  summary: "Summary",
  patient_message: "Patient message",
};

export function MessagePanel({ conversationId, onConversationCreated }: Props) {
  const [messages, setMessages] = useState<Message[]>([]);
  const [input, setInput] = useState("");
  const [mode, setMode] = useState<ChatMode>("triage");
  const [sending, setSending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const bottomRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);

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
      const result = await postChat(text, conversationId ?? undefined, mode);

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
                  className={`rounded-2xl px-4 py-2 text-sm leading-relaxed ${
                    m.role === "user"
                      ? "max-w-[70%] bg-zinc-900 text-zinc-50 dark:bg-zinc-50 dark:text-zinc-950"
                      : "w-full max-w-[80%] bg-zinc-100 text-zinc-900 dark:bg-zinc-800 dark:text-zinc-50"
                  }`}
                >
                  {m.role === "user" ? (
                    m.content
                  ) : (
                    <AssistantBubble content={m.content} />
                  )}
                </div>
              </div>
            ))}
            <div ref={bottomRef} />
          </div>
        )}
      </div>

      {/* Input bar — always visible; sending with no conversation creates one */}
      <div className="border-t border-zinc-200 bg-white px-4 pb-4 pt-3 dark:border-zinc-800 dark:bg-zinc-950">
        {/* PII warning — always visible, not dismissible */}
        <div className="mb-2 flex items-center gap-2 rounded-md bg-amber-50 px-3 py-1.5 text-xs text-amber-700 dark:bg-amber-950/40 dark:text-amber-400">
          <span aria-hidden="true">⚠</span>
          <span>Do not paste identifying patient data (name, PESEL, phone, e-mail).</span>
        </div>

        {error && <p className="mb-2 text-xs text-red-500">{error}</p>}

        {/* Mode selector */}
        <div className="mb-2 flex gap-1">
          {(Object.keys(MODE_LABELS) as ChatMode[]).map((m) => (
            <button
              key={m}
              type="button"
              onClick={() => setMode(m)}
              disabled={sending}
              className={`rounded-md px-2.5 py-1 text-xs font-medium transition disabled:opacity-50 ${
                mode === m
                  ? "bg-zinc-900 text-zinc-50 dark:bg-zinc-50 dark:text-zinc-950"
                  : "text-zinc-500 hover:bg-zinc-100 hover:text-zinc-900 dark:text-zinc-400 dark:hover:bg-zinc-800 dark:hover:text-zinc-50"
              }`}
            >
              {MODE_LABELS[m]}
            </button>
          ))}
        </div>

        <form
          onSubmit={(e) => {
            e.preventDefault();
            void handleSend();
          }}
          className="flex items-end gap-2"
        >
          <textarea
            ref={inputRef}
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => {
              // Ctrl/Cmd+Enter submits; plain Enter adds a newline
              if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
                e.preventDefault();
                void handleSend();
              }
            }}
            disabled={sending}
            placeholder="Describe the clinical situation…"
            rows={3}
            className="flex-1 resize-none rounded-lg border border-zinc-200 bg-zinc-50 px-3 py-2 text-sm text-zinc-900 outline-none transition focus:border-zinc-400 disabled:opacity-50 dark:border-zinc-700 dark:bg-zinc-900 dark:text-zinc-50 dark:focus:border-zinc-500"
          />
          <button
            type="submit"
            disabled={sending || !input.trim()}
            className="rounded-lg bg-zinc-900 px-4 py-2 text-sm font-medium text-zinc-50 transition hover:bg-zinc-700 disabled:cursor-not-allowed disabled:opacity-50 dark:bg-zinc-50 dark:text-zinc-950 dark:hover:bg-zinc-200"
          >
            {sending ? "…" : "Send"}
          </button>
        </form>
        <p className="mt-1.5 text-right text-xs text-zinc-400">
          Ctrl+Enter to send
        </p>
      </div>
    </div>
  );
}
