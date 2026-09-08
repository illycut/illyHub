"use client";
import type { PandoraSyncState } from "@/lib/hub/types";
import { PANDORA_SYNC_CHIP } from "@/lib/airplay";

/** Neutral glyph for the bridge: a speaker with an outward wave, never pulsing (nothing to correct). */
export function AirPlayGlyph({ className = "" }: { className?: string }) {
  return (
    <svg viewBox="0 0 12 12" width="12" height="12" aria-hidden="true" focusable="false" className={`block shrink-0 ${className}`} data-shape="airplay">
      <path d="M2 4.5h2.2L7 2v8L4.2 7.5H2Z" fill="currentColor" />
      <path d="M8.6 4a3 3 0 0 1 0 4" fill="none" stroke="currentColor" strokeWidth="1.2" strokeLinecap="round" />
    </svg>
  );
}

/**
 * Pandora Sync chip (Phase 7, experimental): "Pandora Sync" in a neutral tone, glyph + text, no
 * pulse, no live region (nothing here changes over time). Status only: the single Stop lives in
 * Now Playing's meta row (UX U2/U4), so a tap on the compact chip falls through to the mini-player's
 * expand. Hidden when the bridge is inactive.
 */
export function PandoraSyncChip({
  state,
  compact = false,
  hidden = false,
}: {
  state: PandoraSyncState | null | undefined;
  /** Mini-player: same pill, non-interactive. */
  compact?: boolean;
  /** Mini-player while Now Playing is expanded: keep layout, drop from the a11y tree. */
  hidden?: boolean;
}) {
  if (!state?.active) return null;
  return (
    <span
      className="inline-flex shrink-0 items-center gap-1 rounded-pill bg-overlay px-2 py-half text-micro text-secondary"
      data-testid="pandora-sync-chip"
      data-compact={compact ? "true" : undefined}
      aria-hidden={hidden || undefined}
    >
      <AirPlayGlyph className="text-secondary" />
      <span className="whitespace-nowrap">{PANDORA_SYNC_CHIP}</span>
    </span>
  );
}
