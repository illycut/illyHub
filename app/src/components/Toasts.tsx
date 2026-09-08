"use client";
import { useEffect } from "react";
import Link from "next/link";
import { ttlOf, useToasts } from "@/lib/ui/toasts";

/** Command-failed toasts (design system §8). aria-live so screen readers hear them once. */
export function Toasts() {
  const toasts = useToasts((s) => s.toasts);
  const expire = useToasts((s) => s.expire);
  const dismiss = useToasts((s) => s.dismiss);
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
          data-ttl={ttlOf(t)}
          className="pointer-events-auto flex max-w-[420px] items-center gap-3 rounded-control bg-overlay px-4 py-3 text-body text-primary shadow-mini"
        >
          <span className="min-w-0 flex-1">{t.message}</span>
          {t.action ? (
            <Link href={t.action.href} className="shrink-0 rounded-control px-2 py-2 text-body text-signal" onClick={() => dismiss(t.id)} data-testid="toast-action">
              {t.action.label}
            </Link>
          ) : null}
        </div>
      ))}
    </div>
  );
}
