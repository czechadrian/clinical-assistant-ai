"use client";

import { useEffect, useRef, useState } from "react";
import { ApiError, createConversation, getConversationState, getMessages, postChat, withRetry, type AssistantPayload, type ChatMode, type ConversationStateOut } from "@/lib/api";

type Message = {
  id: string;
  role: string;
  content: unknown; // JSONB from Supabase: {text} for user, AssistantPayload for assistant
};

// Handles both new JSONB shape {text: "..."} and legacy plain string.
function getUserText(content: unknown): string {
  if (typeof content === "string") return content;
  if (content && typeof content === "object" && "text" in content)
    return String((content as { text: unknown }).text);
  return "";
}

// Maps stable backend error codes to user-friendly Polish messages.
// Falls back to the raw error message for unknown codes.
const ERROR_MESSAGES: Record<string, string> = {
  PII_DETECTED:
    "Wykryto dane identyfikacyjne pacjenta. Usuń identyfikatory przed wysłaniem.",
  MOCK_DISABLED: "Integracja z modelem AI nie jest jeszcze włączona. Skontaktuj się z administratorem.",
  CONVERSATION_NOT_FOUND: "Nie znaleziono rozmowy. Odśwież stronę i spróbuj ponownie.",
  VALIDATION_FAILED: "Odpowiedź asystenta nie spełnia wymagań schematu. Spróbuj ponownie.",
  TIMEOUT: "Przekroczono czas oczekiwania na odpowiedź. Spróbuj ponownie.",
  TRANSIENT_UPSTREAM: "Usługa chwilowo niedostępna. Spróbuj ponownie za chwilę.",
};

// Human-readable Polish labels for PII category names returned by the backend.
const PII_CATEGORY_LABELS: Record<string, string> = {
  email: "adres e-mail",
  phone: "numer telefonu",
  pesel: "PESEL",
  nip: "NIP",
  date_of_birth: "data urodzenia",
  address: "adres zamieszkania",
};

type PiiSuggestion = {
  categories: string[];
  sanitizedText?: string;
};

function getErrorMessage(err: unknown): string {
  if (err instanceof ApiError && err.code && err.code in ERROR_MESSAGES) {
    return ERROR_MESSAGES[err.code];
  }
  return err instanceof Error ? err.message : "Wysyłanie wiadomości nie powiodło się.";
}

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

// Shown only outside production — lets developers verify which chunks drove the response.
const IS_DEV = process.env.NODE_ENV !== "production";

function AssistantBubble({ content }: { content: unknown }) {
  const [showContext, setShowContext] = useState(false);

  let payload: AssistantPayload | null = null;
  if (typeof content === "string") {
    // Legacy TEXT rows that haven't been migrated yet
    try { payload = JSON.parse(content) as AssistantPayload; } catch { /* fallback */ }
  } else if (content && typeof content === "object") {
    // Normal JSONB path — already a JS object, no parse needed
    payload = content as AssistantPayload;
  }

  if (!payload) {
    return <span>{getUserText(content)}</span>;
  }

  // Dev-only: read workflow metadata from _meta in stored message content.
  // Never shown in production — no patient impact.
  const meta = IS_DEV
    ? ((content as Record<string, unknown>)?._meta as Record<string, unknown> | undefined)
    : undefined;
  const workflowName = meta?.workflow_name as string | undefined;
  const routerReason = meta?.router_reason as string | undefined;
  // [optional debug] Tool metadata — Day 23. IDs only; never snippet content.
  const toolUsed = meta?.tool_used as boolean | undefined;
  const toolName = meta?.tool_name as string | undefined;
  const retrievedIds = meta?.retrieved_chunk_ids as string[] | undefined;

  return (
    <div className="space-y-3 text-sm">
      {/* Flag badge */}
      <span
        className={`inline-block rounded-full px-2 py-0.5 text-xs font-medium ${FLAG_STYLES[payload.flag]}`}
      >
        {FLAG_LABELS[payload.flag]}
      </span>

      {/* Workflow badge — dev/admin only */}
      {IS_DEV && workflowName && (
        <span className="ml-2 inline-block rounded border border-slate-200 bg-slate-50 px-1.5 py-0.5 font-mono text-xs text-slate-500 dark:border-slate-700 dark:bg-slate-800 dark:text-slate-400">
          {workflowName}
          {routerReason && routerReason !== `mode_${workflowName}` && (
            <span className="text-slate-400"> · {routerReason}</span>
          )}
        </span>
      )}

      {/* [optional] Tool debug badge — dev only. Shows tool name + retrieved IDs. */}
      {IS_DEV && toolUsed && (
        <span className="ml-2 inline-block rounded border border-violet-200 bg-violet-50 px-1.5 py-0.5 font-mono text-xs text-violet-600 dark:border-violet-800 dark:bg-violet-950/30 dark:text-violet-400">
          🔍 {toolName ?? "tool"}
          {retrievedIds && retrievedIds.length > 0 && (
            <span className="text-violet-400"> · {retrievedIds.length} chunk{retrievedIds.length !== 1 ? "s" : ""}</span>
          )}
        </span>
      )}

      {/* Urgent escalation banner — shown when red flags present (code-enforced, not prompt-only) */}
      {payload.red_flags.length > 0 && (
        <div className="rounded-lg border-2 border-red-500 bg-red-100 px-3 py-2 dark:border-red-600 dark:bg-red-950/60">
          <p className="mb-1 text-sm font-bold uppercase tracking-wide text-red-700 dark:text-red-400">
            ⚠ Wysoki priorytet — pilna eskalacja
          </p>
          <p className="mb-2 text-xs text-red-700 dark:text-red-300">
            Wykryto objawy wymagające natychmiastowej oceny. Proszę działać zgodnie z poniższymi wskazówkami.
          </p>
          <ul className="space-y-1">
            {payload.red_flags.map((f, i) => (
              <li key={i} className="flex gap-2 text-xs font-medium text-red-800 dark:text-red-200">
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
          <div className="mb-1 flex items-center justify-between">
            <p className="text-xs font-semibold uppercase tracking-wide text-zinc-500 dark:text-zinc-400">
              Sources
            </p>
            {IS_DEV && payload.sources.some((s) => s.text_snippet) && (
              <button
                type="button"
                onClick={() => setShowContext((v) => !v)}
                className="text-xs text-zinc-400 underline-offset-2 hover:text-zinc-600 hover:underline dark:hover:text-zinc-300"
              >
                {showContext ? "Hide context" : "Show context"}
              </button>
            )}
          </div>
          <ul className="space-y-1.5">
            {payload.sources.map((src, i) => (
              <li key={src.id} className="text-xs">
                <span className="font-medium text-zinc-700 dark:text-zinc-300">{src.title}</span>
                {src.section && (
                  <span className="text-zinc-500 dark:text-zinc-400"> · {src.section}</span>
                )}
                <span className="ml-1.5 font-mono text-zinc-400 dark:text-zinc-600">
                  [{i + 1}]
                </span>
                {showContext && src.text_snippet && (
                  <p className="mt-0.5 rounded border border-zinc-200 bg-zinc-50 px-2 py-1 font-mono text-zinc-500 dark:border-zinc-700 dark:bg-zinc-900 dark:text-zinc-400">
                    {src.text_snippet}
                  </p>
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
  doc_qa: "Guideline Q&A",
};

// Generic placeholder templates — fill the textarea on click.
// No real medical content; clinician replaces bracketed values.
const TEMPLATES: Record<ChatMode, string[]> = {
  triage: [
    "Patient presents with [symptom]. Duration: [time]. Vitals: BP [X/Y], HR [N], SpO2 [N]%.",
    "Elderly patient, acute onset [symptom]. Known conditions: [list]. Current medications: [list].",
    "Post-operative day [N]. Chief complaint: [symptom]. Temperature [X]°C, BP [X/Y].",
  ],
  summary: [
    "Summarize visit: complaint [X], examination findings [Y], diagnosis [Z], plan [W].",
    "Discharge summary for patient with [diagnosis]. Treatment: [treatment]. Follow-up: [plan].",
    "Referral note to [specialty]: patient with [condition], reason for referral [reason].",
  ],
  patient_message: [
    "Explain [diagnosis] in simple language and describe the prescribed treatment.",
    "Post-procedure instructions for [procedure] — what to expect and when to seek help.",
    "Side effects of [medication] the patient should monitor and report.",
  ],
  doc_qa: [
    "What is the first-line treatment for [condition] per current ESC/PTK guidelines?",
    "What are the diagnostic criteria for [condition]?",
    "What is the recommended dosing and monitoring for [drug] in [indication]?",
  ],
};

export function MessagePanel({ conversationId, onConversationCreated }: Props) {
  const [messages, setMessages] = useState<Message[]>([]);
  const [input, setInput] = useState("");
  const [newTitle, setNewTitle] = useState("");
  const [mode, setMode] = useState<ChatMode>("triage");
  const [sending, setSending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [piiSuggestion, setPiiSuggestion] = useState<PiiSuggestion | null>(null);
  const [conversationState, setConversationState] = useState<ConversationStateOut | null>(null);
  const [showMemoryPanel, setShowMemoryPanel] = useState(false);
  const bottomRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);

  // Fetch messages whenever the selected conversation changes.
  // Also clear stale UI state so errors/title from a previous panel don't bleed over.
  useEffect(() => {
    setError(null);
    setPiiSuggestion(null);
    setConversationState(null);
    setShowMemoryPanel(false);
    setNewTitle("");

    if (!conversationId) {
      setMessages([]);
      return;
    }

    let cancelled = false;

    getMessages(conversationId).then((data) => {
      if (cancelled) return;
      setMessages(data);
    }).catch((err) => {
      if (cancelled) return;
      setError(err instanceof Error ? err.message : "Failed to load messages");
    });

    // Load conversation memory (dev-only feature — no-op in production).
    if (IS_DEV) {
      getConversationState(conversationId).then((state) => {
        if (cancelled) return;
        setConversationState(state);
      }).catch(() => { /* memory is non-critical — silent failure */ });
    }

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
    setPiiSuggestion(null);
    setInput("");

    // One idempotency key per send attempt — ensures exactly-once delivery even
    // if the user retries or the client retries internally via withRetry.
    const idempotencyKey = crypto.randomUUID();

    // Optimistic user bubble — shown immediately before any network calls.
    const optimisticUser: Message = {
      id: crypto.randomUUID(),
      role: "user",
      content: { text, mode },
    };
    setMessages((prev) => [...prev, optimisticUser]);

    try {
      // If there's no conversation yet, create one silently first.
      // onConversationCreated is called AFTER postChat so that when the parent
      // triggers a re-fetch of messages (via useEffect on conversationId change),
      // both messages are already in the DB and the fetch returns them correctly.
      let convId = conversationId;
      let createdId: string | null = null;
      if (!convId) {
        const title = newTitle.trim() || text.slice(0, 60);
        const conv = await createConversation(title);
        convId = conv.id;
        createdId = conv.id;
      }

      // withRetry handles transient network blips (status 0, 429, 502, 503, 504).
      // The idempotency key makes retries safe — the backend deduplicates them.
      const result = await withRetry(() => postChat(text, convId!, mode, idempotencyKey));

      // Replace the optimistic user bubble and append the assistant reply.
      const optimisticAssistant: Message = {
        id: crypto.randomUUID(),
        role: "assistant",
        content: result.assistant_payload,
      };
      setMessages((prev) => [...prev.slice(0, -1), optimisticUser, optimisticAssistant]);

      // Notify parent now that messages are persisted — the resulting
      // conversationId prop change triggers useEffect which re-fetches
      // from DB (already populated) without a visible flash.
      if (createdId) onConversationCreated(createdId);

      // Refresh memory panel (dev only) after each assistant response.
      if (IS_DEV && convId) {
        getConversationState(convId).then((state) => {
          setConversationState(state);
        }).catch(() => { /* non-critical */ });
      }
    } catch (err) {
      // Roll back the optimistic message and restore the draft.
      setMessages((prev) => prev.filter((m) => m.id !== optimisticUser.id));
      setError(getErrorMessage(err));
      setInput(text);
      // Extract PII suggestion from backend (only present when pii_suggest_mode is enabled).
      if (err instanceof ApiError && err.code === "PII_DETECTED" && err.extra) {
        const cats = err.extra.pii_categories;
        const san = err.extra.sanitized_text;
        setPiiSuggestion({
          categories: Array.isArray(cats) ? (cats as string[]) : [],
          sanitizedText: typeof san === "string" ? san : undefined,
        });
      }
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
          <div className="flex flex-col items-center gap-4 pt-12">
            {!conversationId && (
              <div className="w-full max-w-sm">
                <label className="mb-1 block text-xs font-medium text-zinc-500 dark:text-zinc-400">
                  Conversation title <span className="font-normal text-zinc-400">(optional)</span>
                </label>
                <input
                  type="text"
                  value={newTitle}
                  onChange={(e) => setNewTitle(e.target.value)}
                  placeholder="e.g. 67M chest pain triage"
                  maxLength={200}
                  className="w-full rounded-lg border border-zinc-200 bg-zinc-50 px-3 py-2 text-sm text-zinc-900 outline-none transition focus:border-zinc-400 dark:border-zinc-700 dark:bg-zinc-900 dark:text-zinc-50 dark:focus:border-zinc-500"
                />
              </div>
            )}
            <p className="text-sm text-zinc-400">
              {conversationId ? "No messages yet." : "Type a message below to begin."}
            </p>
          </div>
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
                    getUserText(m.content)
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

      {/* Conversation context panel — dev/admin only */}
      {IS_DEV && conversationState && conversationState.summary && (
        <div className="border-t border-zinc-200 bg-zinc-50 px-4 py-2 text-xs dark:border-zinc-800 dark:bg-zinc-900/50">
          <button
            type="button"
            onClick={() => setShowMemoryPanel((v) => !v)}
            className="flex w-full items-center gap-1.5 text-left text-zinc-500 hover:text-zinc-700 dark:text-zinc-400 dark:hover:text-zinc-200"
          >
            <span className="font-mono text-zinc-400 dark:text-zinc-600">{showMemoryPanel ? "▾" : "▸"}</span>
            <span className="font-medium">Conversation context</span>
            <span className="ml-auto font-mono text-zinc-400">{conversationState.summary.length}c</span>
          </button>
          {showMemoryPanel && (
            <div className="mt-2 space-y-2 rounded-md border border-zinc-200 bg-white p-2 dark:border-zinc-700 dark:bg-zinc-900">
              <div>
                <p className="mb-0.5 font-semibold uppercase tracking-wide text-zinc-400 dark:text-zinc-500">Summary</p>
                <p className="font-mono text-zinc-600 dark:text-zinc-300">{conversationState.summary}</p>
              </div>
              {conversationState.open_questions.length > 0 && (
                <div>
                  <p className="mb-0.5 font-semibold uppercase tracking-wide text-zinc-400 dark:text-zinc-500">Open questions</p>
                  <ul className="space-y-0.5">
                    {conversationState.open_questions.map((q, i) => (
                      <li key={i} className="text-zinc-600 dark:text-zinc-300">? {q}</li>
                    ))}
                  </ul>
                </div>
              )}
              {conversationState.known_constraints.length > 0 && (
                <div>
                  <p className="mb-0.5 font-semibold uppercase tracking-wide text-zinc-400 dark:text-zinc-500">Known constraints</p>
                  <ul className="space-y-0.5">
                    {conversationState.known_constraints.map((c, i) => (
                      <li key={i} className="text-zinc-600 dark:text-zinc-300">▲ {c}</li>
                    ))}
                  </ul>
                </div>
              )}
              <p className="text-right text-zinc-400 dark:text-zinc-600">Updated {conversationState.updated_at.slice(0, 19).replace("T", " ")} UTC</p>
            </div>
          )}
        </div>
      )}

      {/* Input bar — always visible; sending with no conversation creates one */}
      <div className="border-t border-zinc-200 bg-white px-4 pb-4 pt-3 dark:border-zinc-800 dark:bg-zinc-950">
        {/* PII warning — always visible, not dismissible */}
        <div className="mb-2 flex items-center gap-2 rounded-md bg-amber-50 px-3 py-1.5 text-xs text-amber-700 dark:bg-amber-950/40 dark:text-amber-400">
          <span aria-hidden="true">⚠</span>
          <span>Do not paste identifying patient data (name, PESEL, phone, e-mail).</span>
        </div>

        {error && (
          <div className="mb-2">
            <p className="text-xs text-red-500">{error}</p>
            {piiSuggestion && piiSuggestion.categories.length > 0 && (
              <div className="mt-1.5 rounded-md border border-red-200 bg-red-50 p-2 text-xs dark:border-red-800 dark:bg-red-950/30">
                <p className="font-medium text-red-700 dark:text-red-400">
                  Wykryte typy danych:{" "}
                  {piiSuggestion.categories
                    .map((c) => PII_CATEGORY_LABELS[c] ?? c)
                    .join(", ")}
                </p>
                <p className="mt-0.5 text-red-600 dark:text-red-400">
                  Usuń lub zastąp identyfikatory, a następnie wyślij ponownie.
                </p>
                {piiSuggestion.sanitizedText && (
                  <details className="mt-1.5">
                    <summary className="cursor-pointer select-none font-medium text-red-600 hover:text-red-800 dark:text-red-400 dark:hover:text-red-300">
                      Podgląd po usunięciu identyfikatorów
                    </summary>
                    <div className="mt-1 flex items-start gap-2">
                      <pre className="flex-1 whitespace-pre-wrap rounded border border-red-200 bg-white px-2 py-1.5 font-mono text-red-800 dark:border-red-800 dark:bg-zinc-900 dark:text-red-300">
                        {piiSuggestion.sanitizedText}
                      </pre>
                      <button
                        type="button"
                        onClick={() => setInput(piiSuggestion.sanitizedText!)}
                        className="shrink-0 rounded border border-red-300 px-2 py-1 text-xs text-red-600 transition hover:border-red-500 hover:bg-red-100 dark:border-red-700 dark:text-red-400 dark:hover:bg-red-950/60"
                      >
                        Użyj
                      </button>
                    </div>
                  </details>
                )}
              </div>
            )}
          </div>
        )}

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

        {/* Template buttons — click to pre-fill the textarea */}
        <div className="mb-2 flex flex-wrap gap-1">
          {TEMPLATES[mode].map((tpl, i) => (
            <button
              key={i}
              type="button"
              disabled={sending}
              onClick={() => setInput(tpl)}
              className="rounded-md border border-zinc-200 px-2 py-1 text-xs text-zinc-500 transition hover:border-zinc-400 hover:text-zinc-800 disabled:opacity-40 dark:border-zinc-700 dark:text-zinc-400 dark:hover:border-zinc-500 dark:hover:text-zinc-200"
            >
              Template {i + 1}
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
