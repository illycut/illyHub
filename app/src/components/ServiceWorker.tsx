"use client";
import { useEffect } from "react";

/** Registers /sw.js in production over a secure context. Version-stamped so a new build replaces the shell cache. */
export function ServiceWorker({ version }: { version: string }) {
  useEffect(() => {
    if (process.env.NODE_ENV !== "production") return;
    if (typeof navigator === "undefined" || !("serviceWorker" in navigator)) return;
    if (!window.isSecureContext) return;
    navigator.serviceWorker.register(`/sw.js?v=${encodeURIComponent(version)}`).catch(() => {
      // offline shell is a convenience, not a requirement
    });
  }, [version]);
  return null;
}
