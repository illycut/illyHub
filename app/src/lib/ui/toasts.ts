/**
 * Command-failed toasts (design system §8): plain statement of what did not happen, auto-dismiss
 * after 4 s, never more than two on screen (newest replaces oldest). A toast may carry one action
 * (e.g. "Connect Tidal" deep-linking into Settings).
 */
import { create } from "zustand";

export interface ToastAction {
  label: string;
  href: string;
}

export interface Toast {
  id: number;
  message: string;
  createdAt: number;
  action: ToastAction | null;
}

export const TOAST_TTL_MS = 4000;
/** Toasts with an action stay long enough to tap it. */
export const TOAST_ACTION_TTL_MS = 8000;
export const TOAST_MAX = 2;

interface ToastStore {
  toasts: Toast[];
  push(message: string, opts?: { now?: number; action?: ToastAction | null }): number;
  dismiss(id: number): void;
  expire(now: number): void;
}

let seq = 0;

export function ttlOf(t: Toast): number {
  return t.action ? TOAST_ACTION_TTL_MS : TOAST_TTL_MS;
}

export const useToasts = create<ToastStore>((set) => ({
  toasts: [],
  push(message, opts = {}) {
    const id = ++seq;
    set((s) => {
      const next = [...s.toasts, { id, message, createdAt: opts.now ?? Date.now(), action: opts.action ?? null }];
      return { toasts: next.slice(-TOAST_MAX) };
    });
    return id;
  },
  dismiss(id) {
    set((s) => ({ toasts: s.toasts.filter((t) => t.id !== id) }));
  },
  expire(now) {
    set((s) => {
      const kept = s.toasts.filter((t) => now - t.createdAt < ttlOf(t));
      return kept.length === s.toasts.length ? s : { toasts: kept };
    });
  },
}));

export function toast(message: string, action?: ToastAction): number {
  return useToasts.getState().push(message, { action: action ?? null });
}
