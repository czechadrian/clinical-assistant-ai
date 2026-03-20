"use client";

import { ReactNode, useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { supabase } from "@/lib/supabaseClient";

// Inverse of AuthGate: if a session exists, redirect away from /login.
// Prevents logged-in users from seeing the login form.
export function LoginGuard({ children }: { children: ReactNode }) {
  const router = useRouter();
  const [checking, setChecking] = useState(true);

  useEffect(() => {
    let isMounted = true;

    const {
      data: { subscription },
    } = supabase.auth.onAuthStateChange((event, session) => {
      if (!isMounted) return;

      if (event === "INITIAL_SESSION" || event === "SIGNED_IN") {
        if (session) {
          router.replace("/app");
        } else {
          setChecking(false);
        }
        return;
      }

      if (event === "SIGNED_OUT") {
        setChecking(false);
      }
    });

    return () => {
      isMounted = false;
      subscription.unsubscribe();
    };
  }, [router]);

  if (checking) {
    return (
      <div className="flex min-h-screen items-center justify-center bg-zinc-50 font-sans dark:bg-black">
        <div className="rounded-full border border-zinc-200 bg-white px-4 py-2 text-sm text-zinc-600 shadow-sm dark:border-zinc-800 dark:bg-zinc-950 dark:text-zinc-300">
          Checking authentication…
        </div>
      </div>
    );
  }

  return <>{children}</>;
}
