"use client";
import { useEffect } from "react";
import { TOAST_TTL_MS, useToasts } from "@/lib/ui/toasts";

/** Command-failed toasts (design system §8). aria-live so screen readers hear them once. */
export function Toasts() {
  const toasts = useToasts((s) => s.toasts);
  const expire = useToasts((s) => s.expire);
  useEffect(() => {
    if (toasts.length === 0) return;
    const id = setInterval(() => expire(Date.now()), 250);
    return () => clearInterval(id);
  }, [toasts.length, expire]);
  return (
    <div
      aria-live="polite"
      aria-atomic="false"
      className="pointer-events-none fixed inset-x-0 z-50 flex flex-col items-center gap-2 px-4"
      style={{ bottom: "calc(var(--size-mini-player) + var(--safe-bottom) + 12px)" }}
    >
      {toasts.map((t) => (
        <div
          key={t.id}
          role="status"
          data-ttl={TOAST_TTL_MS}
          className="pointer-events-auto max-w-[420px] rounded-control bg-overlay px-4 py-3 text-body text-primary shadow-mini"
        >
          {t.message}
        </div>
      ))}
    </div>
  );
}
