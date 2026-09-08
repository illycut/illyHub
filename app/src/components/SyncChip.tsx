"use client";
import { useState } from "react";
import type { SyncState } from "@/lib/hub/types";
import { RETRY, STOP_SYNC, chipModel, type ChipShape } from "@/lib/sync";
import { useReducedMotion } from "@/lib/reducedMotion";

/** One inline glyph per chip: shape carries the state alongside the token color (§9). */
export function SyncGlyph({ shape, className = "", pulse = false }: { shape: ChipShape; className?: string; pulse?: boolean }) {
  return (
    <svg
      viewBox="0 0 12 12"
      width="12"
      height="12"
      aria-hidden="true"
      focusable="false"
      className={`block shrink-0 ${className} ${pulse ? "pulse-once" : ""}`}
      data-shape={shape}
    >
      {shape === "filled" ? <circle cx="6" cy="6" r="5" fill="currentColor" /> : null}
      {shape === "half" ? (
        <>
          <circle cx="6" cy="6" r="4.75" fill="none" stroke="currentColor" strokeWidth="1.5" />
          <path d="M6 1.25 A4.75 4.75 0 0 1 6 10.75 Z" fill="currentColor" />
        </>
      ) : null}
      {shape === "ring" ? <circle cx="6" cy="6" r="4.75" fill="none" stroke="currentColor" strokeWidth="1.5" /> : null}
      {shape === "dotted" ? <circle cx="6" cy="6" r="4.75" fill="none" stroke="currentColor" strokeWidth="1.5" strokeDasharray="2 2.2" /> : null}
    </svg>
  );
}

/**
 * Sync chip (design system §6.7): hidden when idle or stopped; glyph + text for "Starting"
 * (resolving/priming/verifying/starting), "Synced" (locked), "Adjusting" (drifting/correcting)
 * and "Sync lost". One 400ms pulse on the glyph per correction event, keyed on
 * `last_correction_at`, never looping; a static color change under reduced motion (§7).
 *
 * The chip has no live region: PlayerChrome owns the single announcer. When lost, the full
 * variant shows the chip plus separate "Retry" (text-primary) and "Stop sync" text buttons;
 * the compact (mini-player) variant is one button reading "Sync lost · Retry".
 */
export function SyncChip({
  sync,
  onRetry,
  onStop,
  compact = false,
  hidden = false,
}: {
  sync: SyncState | null | undefined;
  onRetry?: () => void;
  onStop?: () => void;
  /** Mini-player: single compact control. */
  compact?: boolean;
  /** Mini-player while Now Playing is expanded: keep layout, drop from the a11y tree. */
  hidden?: boolean;
}) {
  const correction = sync?.last_correction_at ?? null;
  // Pulse once per correction event: any correction newer than the one we mounted with.
  const [initial] = useState(correction);
  const reduced = useReducedMotion();
  const pulse = !reduced && correction !== null && correction !== initial;
  const m = chipModel(sync);
  if (!m) return null;
  const cls = "inline-flex shrink-0 items-center gap-1 rounded-pill bg-overlay px-2 py-half text-micro text-secondary";
  const glyph = <SyncGlyph key={correction ?? "none"} shape={m.shape} className={m.tone} pulse={pulse} />;
  if (m.lost && compact) {
    return (
      <button
        type="button"
        className={`${cls} min-h-target justify-center`}
        onClick={onRetry}
        disabled={!onRetry}
        data-testid="sync-retry"
        data-status={sync!.status}
        aria-hidden={hidden || undefined}
        tabIndex={hidden ? -1 : undefined}
      >
        {glyph}
        <span className="whitespace-nowrap">{m.text} · {RETRY}</span>
      </button>
    );
  }
  if (m.lost) {
    return (
      <span className="inline-flex shrink-0 items-center gap-gap-min" data-testid="sync-chip" data-status={sync!.status} aria-hidden={hidden || undefined}>
        <span className={cls}>
          {glyph}
          <span className="whitespace-nowrap">{m.text}</span>
        </span>
        <button type="button" className="min-h-target min-w-target rounded-control px-3 text-micro text-primary" onClick={onRetry} disabled={!onRetry} data-testid="sync-retry">
          {RETRY}
        </button>
        {onStop ? (
          <button type="button" className="min-h-target min-w-target rounded-control px-3 text-micro text-secondary" onClick={onStop} data-testid="sync-stop">
            {STOP_SYNC}
          </button>
        ) : null}
      </span>
    );
  }
  return (
    <span className={cls} data-testid="sync-chip" data-status={sync!.status} aria-hidden={hidden || undefined}>
      {glyph}
      <span className="whitespace-nowrap">{m.text}</span>
    </span>
  );
}
