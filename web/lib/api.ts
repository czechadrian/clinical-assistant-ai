import { supabase } from "@/lib/supabaseClient";

// NEXT_PUBLIC_API_BASE_URL is the canonical name; NEXT_PUBLIC_BACKEND_URL kept
// for backwards-compatibility with existing .env.local files.
const BACKEND_URL =
  process.env.NEXT_PUBLIC_API_BASE_URL ??
  process.env.NEXT_PUBLIC_BACKEND_URL ??
  "http://localhost:8000";

// ---------------------------------------------------------------------------
// Error types
//
// Typed errors let callers react specifically:
//   catch (e) { if (e instanceof NoSessionError) router.replace("/login") }
// ---------------------------------------------------------------------------

export class NoSessionError extends Error {
  constructor() {
    super("No active session — user must log in");
    this.name = "NoSessionError";
  }
}

export class ApiError extends Error {
  constructor(
    message: string,
    public readonly status: number,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

// ---------------------------------------------------------------------------
// Token helpers
// ---------------------------------------------------------------------------

// getSession() reads from localStorage — fast, no network call.
// The SDK keeps the token fresh in the background via autoRefreshToken,
// so this is valid in the vast majority of cases.
async function getAccessToken(): Promise<string> {
  const {
    data: { session },
  } = await supabase.auth.getSession();
  if (!session) throw new NoSessionError();
  return session.access_token;
}

// Called only when the server rejects the token with 401.
// Forces a network round-trip to Supabase to get a fresh token.
async function refreshAccessToken(): Promise<string> {
  const {
    data: { session },
    error,
  } = await supabase.auth.refreshSession();
  if (error || !session) throw new NoSessionError();
  return session.access_token;
}

// ---------------------------------------------------------------------------
// apiFetch
//
// Drop-in replacement for fetch() for authenticated backend calls.
// Handles: token injection, 401 retry with refresh, typed error parsing.
//
// Usage:
//   const data = await apiFetch<MyType>("/some-endpoint", {
//     method: "POST",
//     body: JSON.stringify({ key: "value" }),
//   });
// ---------------------------------------------------------------------------

export async function apiFetch<T = unknown>(
  path: string,
  init: RequestInit = {},
): Promise<T> {
  let token = await getAccessToken();

  const request = (t: string): Promise<Response> =>
    fetch(`${BACKEND_URL}${path}`, {
      ...init,
      headers: {
        // Caller can override Content-Type (e.g. multipart) but never Authorization.
        "Content-Type": "application/json",
        ...init.headers,
        Authorization: `Bearer ${t}`, // always last — cannot be overridden by caller
      },
    });

  let res = await request(token);

  // The server rejected the token. This happens when:
  // - The token expired between our getSession() call and the server validating it.
  // - The background refresh hadn't run yet.
  // Force a fresh token from Supabase and retry exactly once.
  if (res.status === 401) {
    token = await refreshAccessToken();
    res = await request(token);
  }

  if (!res.ok) {
    const body = await res.json().catch(() => null);
    throw new ApiError(
      // FastAPI puts error detail in body.detail; fall back to status text
      body?.detail ?? `${res.status} ${res.statusText}`,
      res.status,
    );
  }

  return res.json() as Promise<T>;
}

// ---------------------------------------------------------------------------
// API functions
//
// Each function is just: endpoint + body. Auth boilerplate lives in apiFetch.
// ---------------------------------------------------------------------------

export type MessageOut = {
  id: string;
  conversation_id: string;
  role: string;
  content: Record<string, unknown>; // JSONB: {text, mode} for user, AssistantPayload for assistant
};

export type ConversationOut = {
  id: string;
  title: string | null;
  created_at: string;
  updated_at: string;
};

export type Source = {
  id: string;
  title: string;
  section: string;
};

export type AssistantPayload = {
  questions_to_ask: string[];
  red_flags: string[];
  possible_next_steps: string[];
  patient_facing_summary: string;
  sources: Source[];
  flag: "safe" | "uncertain" | "refuse";
  disclaimer: string;
};

export type ChatMode = "triage" | "summary" | "patient_message";

export type ResponseMetadata = {
  is_mock: boolean;
  model: string;
  prompt_version: string;
};

export type ChatApiResponse = {
  request_id: string;
  assistant_payload: AssistantPayload;
  response_metadata: ResponseMetadata;
};

export async function createConversation(title: string): Promise<ConversationOut> {
  return apiFetch<ConversationOut>("/conversations", {
    method: "POST",
    body: JSON.stringify({ title }),
  });
}

export async function listConversations(): Promise<ConversationOut[]> {
  return apiFetch<ConversationOut[]>("/conversations");
}

export async function getMessages(conversationId: string): Promise<MessageOut[]> {
  return apiFetch<MessageOut[]>(`/conversations/${conversationId}/messages`);
}

export async function postChat(
  inputText: string,
  conversationId: string,
  mode: ChatMode = "triage",
): Promise<ChatApiResponse> {
  return apiFetch<ChatApiResponse>("/chat", {
    method: "POST",
    body: JSON.stringify({
      mode,
      input_text: inputText,
      conversation_id: conversationId,
    }),
  });
}
