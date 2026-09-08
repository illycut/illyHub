/**
 * Command-failed toasts (design system §8): plain statement of what did not happen, auto-dismiss
 * after 4 s, never more than two on screen (newest replaces oldest).
 */
import { create } from "zustand";

export interface Toast {
  id: number;
  message: string;
  createdAt: number;
}

export const TOAST_TTL_MS = 4000;
export const TOAST_MAX = 2;

interface ToastStore {
  toasts: Toast[];
  push(message: string, now?: number): number;
  dismiss(id: number): void;
  expire(now: number): void;
}

let seq = 0;

export const useToasts = create<ToastStore>((set) => ({
  toasts: [],
  push(message, now = Date.now()) {
    const id = ++seq;
    set((s) => {
      const next = [...s.toasts, { id, message, createdAt: now }];
      return { toasts: next.slice(-TOAST_MAX) };
    });
    return id;
  },
  dismiss(id) {
    set((s) => ({ toasts: s.toasts.filter((t) => t.id !== id) }));
  },
  expire(now) {
    set((s) => {
      const kept = s.toasts.filter((t) => now - t.createdAt < TOAST_TTL_MS);
      return kept.length === s.toasts.length ? s : { toasts: kept };
    });
  },
}));

export function toast(message: string): number {
  return useToasts.getState().push(message);
}
