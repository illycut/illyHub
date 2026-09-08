"use client";
import { useState } from "react";
import type { SyncState } from "@/lib/hub/types";

/**
 * Sync chip (design system §6.7): null when idle; dot + "Synced" / "Adjusting" / "Sync lost".
 * One 400ms pulse per correction event, never looping. Glyph shape + text, never color alone.
 * Phase 4 wires the hub's sync engine; the component reads `HubState.sync` already.
 */
export function SyncChip({ sync, onRetry }: { sync: SyncState | null | undefined; onRetry?: () => void }) {
  const correction = sync?.last_correction_at ?? null;
  // Pulse once per correction event: any correction newer than the one we mounted with.
  const [initial] = useState(correction);
  const pulse = correction !== null && correction !== initial;
  if (!sync || sync.status === "idle") return null;
  const map = {
    priming: { dot: "bg-sync-drift", text: "Starting", glyph: "◌" },
    locked: { dot: "bg-sync-locked", text: "Synced", glyph: "●" },
    drifting: { dot: "bg-sync-drift", text: "Adjusting", glyph: "◐" },
    lost: { dot: "bg-sync-lost", text: "Sync lost", glyph: "○" },
  } as const;
  const m = map[sync.status];
  const body = (
    <>
      <span key={correction ?? "none"} className={`block h-2 w-2 rounded-pill ${m.dot} ${pulse ? "pulse-once" : ""}`} aria-hidden="true" />
      <span aria-hidden="true" className="text-micro">
        {m.glyph}
      </span>
      <span>{m.text}</span>
    </>
  );
  const cls = "inline-flex items-center gap-1 rounded-pill bg-overlay px-2 py-half text-micro text-secondary";
  if (sync.status === "lost" && onRetry) {
    return (
      <button type="button" className={`${cls} hit-target justify-center`} onClick={onRetry} data-testid="sync-chip" data-status={sync.status}>
        {body}
        <span className="sr-only">, tap to retry</span>
      </button>
    );
  }
  return (
    <span className={cls} role="status" data-testid="sync-chip" data-status={sync.status}>
      {body}
    </span>
  );
}
