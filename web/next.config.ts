import type { NextConfig } from "next";

// Read the API base URL at build time so it can be included in CSP connect-src.
// Falls back to localhost for local dev builds.
const apiBaseUrl =
  process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000";

const nextConfig: NextConfig = {
  async headers() {
    return [
      {
        source: "/(.*)",
        headers: [
          // Prevent MIME-type sniffing
          { key: "X-Content-Type-Options", value: "nosniff" },
          // Disallow embedding in iframes (clickjacking)
          { key: "X-Frame-Options", value: "DENY" },
          // Minimal referrer leak
          {
            key: "Referrer-Policy",
            value: "strict-origin-when-cross-origin",
          },
          // Minimal CSP:
          //   script-src needs 'unsafe-inline' + 'unsafe-eval' for Next.js
          //   style-src needs 'unsafe-inline' for Tailwind
          //   connect-src allows Supabase and the configured API backend
          {
            key: "Content-Security-Policy",
            value: [
              "default-src 'self'",
              "script-src 'self' 'unsafe-inline' 'unsafe-eval'",
              "style-src 'self' 'unsafe-inline'",
              "img-src 'self' data: blob:",
              `connect-src 'self' https://*.supabase.co ${apiBaseUrl}`,
              "font-src 'self'",
              "frame-ancestors 'none'",
            ].join("; "),
          },
        ],
      },
    ];
  },
};

export default nextConfig;
