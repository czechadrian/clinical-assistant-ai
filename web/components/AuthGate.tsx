"use client";

import { ReactNode, useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { supabase } from "@/lib/supabaseClient";

export function AuthGate({ children }: { children: ReactNode }) {
  const router = useRouter();
  const [checking, setChecking] = useState(true);

  useEffect(() => {
    let isMounted = true;

    // Subscribe FIRST, before any async work. onAuthStateChange fires
    // INITIAL_SESSION synchronously once the SDK has restored the session
    // from localStorage or processed a magic-link token in the URL.
    // Calling getUser() before subscribing risks missing the SIGNED_IN event
    // from the magic-link exchange and incorrectly redirecting to /login.
    const {
      data: { subscription },
    } = supabase.auth.onAuthStateChange((event, session) => {
      if (!isMounted) return;

      if (event === "INITIAL_SESSION" || event === "SIGNED_IN") {
        if (!session) {
          router.replace("/login");
        } else {
          setChecking(false);
        }
        return;
      }

      if (event === "SIGNED_OUT") {
        router.replace("/login");
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
