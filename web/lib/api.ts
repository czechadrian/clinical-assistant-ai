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
    public readonly code?: string, // stable backend error code e.g. "PII_DETECTED"
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

  let res: Response;
  try {
    res = await request(token);
  } catch {
    // fetch() throws TypeError when the network is unreachable or the server
    // is down. Convert to ApiError so the UI shows a readable message.
    throw new ApiError(
      "The API service is unreachable. Please try again later.",
      0,
    );
  }

  // The server rejected the token. This happens when:
  // - The token expired between our getSession() call and the server validating it.
  // - The background refresh hadn't run yet.
  // Force a fresh token from Supabase and retry exactly once.
  if (res.status === 401) {
    token = await refreshAccessToken();
    try {
      res = await request(token);
    } catch {
      throw new ApiError(
        "The API service is unreachable. Please try again later.",
        0,
      );
    }
  }

  if (!res.ok) {
    const body = await res.json().catch(() => null);
    // New shape: {"error": {"code": "...", "message": "..."}}
    // Legacy shape: {"detail": "..."}
    const code: string | undefined = body?.error?.code;
    const message: string =
      body?.error?.message ?? body?.detail ?? `${res.status} ${res.statusText}`;
    throw new ApiError(message, res.status, code);
  }

  return res.json() as Promise<T>;
}

// ---------------------------------------------------------------------------
// Client-side retry
//
// Retries only transient failures (network unreachable, 429, 502, 503, 504).
// Does NOT retry 4xx client errors — those require user action (e.g. PII warning).
// Max 2 retries with a fixed 1 s delay (sufficient for brief network blips).
// ---------------------------------------------------------------------------

const _TRANSIENT_STATUSES = new Set([429, 502, 503, 504]);

export async function withRetry<T>(fn: () => Promise<T>, maxRetries = 2): Promise<T> {
  let lastError: unknown;
  for (let attempt = 0; attempt <= maxRetries; attempt++) {
    try {
      return await fn();
    } catch (err) {
      lastError = err;
      if (err instanceof ApiError) {
        // Don't retry client errors or auth failures — they won't self-heal
        if (!_TRANSIENT_STATUSES.has(err.status) && err.status !== 0) throw err;
      }
      if (attempt < maxRetries) {
        await new Promise((resolve) => setTimeout(resolve, 1000));
      }
    }
  }
  throw lastError;
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
  id: string;        // chunk_id — traces to exact indexed chunk
  title: string;
  section: string;
  text_snippet?: string; // first 300 chars of chunk content; populated server-side for debug toggle
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

export type DocOut = {
  id: string;
  title: string;
  filename: string;
  storage_path: string;
  file_hash: string;
  version: string;
  status: "pending" | "indexed" | "failed";
  created_at: string;
  updated_at: string;
};

export async function listDocs(): Promise<DocOut[]> {
  return apiFetch<DocOut[]>("/docs");
}

export async function postChat(
  inputText: string,
  conversationId: string,
  mode: ChatMode = "triage",
  idempotencyKey?: string,
): Promise<ChatApiResponse> {
  return apiFetch<ChatApiResponse>("/chat", {
    method: "POST",
    headers: idempotencyKey ? { "Idempotency-Key": idempotencyKey } : {},
    body: JSON.stringify({
      mode,
      input_text: inputText,
      conversation_id: conversationId,
    }),
  });
}
