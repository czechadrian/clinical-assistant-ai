"use client";

import { ApiStatus } from "@/components/ApiStatus";

export type Conversation = {
  id: string;
  created_at: string;
};

type Props = {
  conversations: Conversation[];
  selectedId: string | null;
  onSelect: (id: string) => void;
  onNew: () => void;
  email: string | null;
  onLogout: () => void;
};

export function ConversationList({
  conversations,
  selectedId,
  onSelect,
  onNew,
  email,
  onLogout,
}: Props) {
  return (
    <aside className="flex h-full w-64 shrink-0 flex-col border-r border-zinc-200 bg-white dark:border-zinc-800 dark:bg-zinc-950">
      {/* Header */}
      <div className="flex items-center justify-between border-b border-zinc-200 px-4 py-3 dark:border-zinc-800">
        <span className="text-sm font-semibold text-zinc-900 dark:text-zinc-50">
          Chats
        </span>
        <button
          onClick={onNew}
          className="rounded-md px-2 py-1 text-xs font-medium text-zinc-500 transition hover:bg-zinc-100 hover:text-zinc-900 dark:text-zinc-400 dark:hover:bg-zinc-800 dark:hover:text-zinc-50"
        >
          + New
        </button>
      </div>

      {/* List */}
      <nav className="flex-1 overflow-y-auto p-2">
        {conversations.length === 0 ? (
          <p className="px-3 py-4 text-xs text-zinc-400">No conversations yet.</p>
        ) : (
          conversations.map((c) => (
            <button
              key={c.id}
              onClick={() => onSelect(c.id)}
              className={`w-full rounded-lg px-3 py-2 text-left text-xs transition ${
                c.id === selectedId
                  ? "bg-zinc-100 font-medium text-zinc-900 dark:bg-zinc-800 dark:text-zinc-50"
                  : "text-zinc-500 hover:bg-zinc-50 hover:text-zinc-900 dark:text-zinc-400 dark:hover:bg-zinc-900 dark:hover:text-zinc-50"
              }`}
            >
              {new Date(c.created_at).toLocaleString()}
            </button>
          ))
        )}
      </nav>

      {/* Footer */}
      <div className="border-t border-zinc-200 p-3 dark:border-zinc-800">
        <ApiStatus />
        <p className="mb-2 mt-1 truncate text-xs text-zinc-400">{email ?? ""}</p>
        <button
          onClick={onLogout}
          className="w-full rounded-lg border border-zinc-200 py-1.5 text-xs font-medium text-zinc-600 transition hover:bg-zinc-50 dark:border-zinc-700 dark:text-zinc-300 dark:hover:bg-zinc-900"
        >
          Log out
        </button>
      </div>
    </aside>
  );
}
