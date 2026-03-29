/**
 * /status — read-only system info page.
 *
 * Server Component: values are embedded at build time via NEXT_PUBLIC_* env vars.
 * No secrets are exposed (all variables here are safe for the browser).
 * Useful for confirming which environment a Vercel preview build is connected to.
 */

const apiBaseUrl =
  process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000";
const appVersion = process.env.NEXT_PUBLIC_APP_VERSION ?? "dev";
const streamingEnabled = process.env.NEXT_PUBLIC_STREAMING_ENABLED === "true";

type Row = { label: string; value: string };

const rows: Row[] = [
  { label: "App version", value: appVersion },
  { label: "API base URL", value: apiBaseUrl },
  { label: "Streaming", value: streamingEnabled ? "Enabled" : "Disabled" },
];

export default function StatusPage() {
  return (
    <main className="mx-auto max-w-lg px-6 py-12">
      <h1 className="mb-6 text-lg font-semibold text-zinc-900 dark:text-zinc-50">
        System status
      </h1>
      <dl className="divide-y divide-zinc-200 rounded-xl border border-zinc-200 dark:divide-zinc-700 dark:border-zinc-700">
        {rows.map(({ label, value }) => (
          <div
            key={label}
            className="flex items-center justify-between px-4 py-3 text-sm"
          >
            <dt className="font-medium text-zinc-500 dark:text-zinc-400">
              {label}
            </dt>
            <dd className="font-mono text-zinc-900 dark:text-zinc-50">
              {value}
            </dd>
          </div>
        ))}
      </dl>
      <p className="mt-4 text-xs text-zinc-400">
        Values are resolved at build time. No secrets are exposed on this page.
      </p>
    </main>
  );
}
