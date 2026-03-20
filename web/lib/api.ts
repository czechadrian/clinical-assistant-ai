import { supabase } from "@/lib/supabaseClient";

const BACKEND_URL = process.env.NEXT_PUBLIC_BACKEND_URL ?? "http://localhost:8000";

// Shape the backend returns for each message
export type MessageOut = {
  id: string;
  conversation_id: string;
  role: string;
  content: string;
};

export type ChatApiResponse = {
  conversation_id: string;
  user_message: MessageOut;
  assistant_message: MessageOut;
};

async function getToken(): Promise<string> {
  const {
    data: { session },
    error,
  } = await supabase.auth.getSession();
  if (error || !session) throw new Error("No active session");
  return session.access_token;
}

export async function postChat(
  message: string,
  conversationId?: string,
): Promise<ChatApiResponse> {
  const token = await getToken();

  const res = await fetch(`${BACKEND_URL}/chat`, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${token}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      message,
      conversation_id: conversationId ?? null,
    }),
  });

  if (!res.ok) {
    const body = await res.json().catch(() => null);
    throw new Error(body?.detail ?? `Request failed: ${res.status}`);
  }

  return res.json();
}
