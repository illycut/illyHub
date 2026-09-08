"use client";
import { useEffect, useRef, useState } from "react";
import { formatTime } from "@/lib/format";
import { interpolatePosition, shouldAnimate } from "@/lib/position";
import { msToPct, pxToMs } from "@/lib/scrub";
import type { PlayState, Position } from "@/lib/hub/types";

/**
 * Touch-scrubbing timeline (design system §6.4). 4px track, 8px while touched, contrast-guarded
 * fill (`--scrub-fill`), invisible 48px hit zone, cue bubble with the target timecode, seek
 * committed on release. When the side cannot seek (radio, HEOS) the track, fill and timecodes
 * stay at full opacity, the thumb is hidden, and the slider is read-only but still focusable.
 */
export function Scrubber({
  position,
  playState,
  durationMs,
  seekable,
  onSeek,
  now = () => Date.now(),
}: {
  position: Position | null | undefined;
  playState: PlayState | undefined;
  durationMs: number | null | undefined;
  seekable: boolean;
  onSeek: (ms: number) => void;
  /** Hub-clock "now"; positions are stamped on the hub's clock. */
  now?: () => number;
}) {
  const trackRef = useRef<HTMLDivElement>(null);
  const [scrubMs, setScrubMs] = useState<number | null>(null);
  // A just-committed seek shows immediately and stays until the hub reports a new position.
  const [committed, setCommitted] = useState<{ ms: number; ref: Position | null | undefined } | null>(null);
  const [, setTick] = useState(0);
  const [announce, setAnnounce] = useState("");

  // Re-render ~4 Hz while playing so the interpolated position advances between hub updates.
  useEffect(() => {
    if (playState !== "play") return;
    const id = setInterval(() => setTick((t) => t + 1), 250);
    return () => clearInterval(id);
  }, [playState]);

  const hubMs = interpolatePosition(position, playState, now(), durationMs);
  const liveMs = committed !== null && committed.ref === position ? committed.ms : hubMs;
  const shown = scrubMs ?? liveMs;
  const pct = msToPct(shown, durationMs);
  const canSeek = seekable && !!durationMs && durationMs > 0;
  const smooth = scrubMs === null && shouldAnimate(position);

  const msFromEvent = (e: React.PointerEvent) => {
    const r = trackRef.current?.getBoundingClientRect();
    if (!r) return 0;
    return pxToMs(e.clientX, r.left, r.width, durationMs ?? 0);
  };
  const onPointerDown = (e: React.PointerEvent) => {
    if (!canSeek) return;
    try {
      (e.currentTarget as HTMLElement).setPointerCapture?.(e.pointerId);
    } catch {
      // some engines throw for synthetic or already-released pointers; dragging still works
    }
    setScrubMs(msFromEvent(e));
  };
  const onPointerMove = (e: React.PointerEvent) => {
    if (!canSeek || scrubMs === null) return;
    setScrubMs(msFromEvent(e));
  };
  const commit = (target: number) => {
    setCommitted({ ms: target, ref: position });
    onSeek(target);
    setAnnounce(`Seeked to ${formatTime(target)}`);
  };
  const onPointerUp = () => {
    if (scrubMs === null) return;
    const target = scrubMs;
    setScrubMs(null);
    commit(target);
  };
  /** A cancelled pointer (scroll took over, system gesture) discards the scrub; no seek. */
  const onPointerCancel = () => {
    setScrubMs(null);
  };
  const onKeyDown = (e: React.KeyboardEvent) => {
    if (!canSeek) return;
    const step = e.shiftKey ? 30_000 : 5_000;
    let target: number | null = null;
    if (e.key === "ArrowRight") target = Math.min(durationMs ?? 0, liveMs + step);
    if (e.key === "ArrowLeft") target = Math.max(0, liveMs - step);
    if (target === null) return;
    e.preventDefault();
    commit(target);
  };

  if (!durationMs) {
    // No duration (a radio stream with an unknown length): no track, no slider; elapsed time only.
    return (
      <div className="flex h-target w-full items-center" data-testid="scrubber" data-seekable="false">
        <span role="timer" aria-label="Elapsed" className="text-micro text-tertiary numeric" data-testid="elapsed">
          {formatTime(shown)}
        </span>
      </div>
    );
  }

  return (
    <div className="w-full" data-testid="scrubber" data-seekable={canSeek}>
      <div
        role="slider"
        tabIndex={0}
        aria-label="Playback position"
        aria-valuemin={0}
        aria-valuemax={durationMs}
        aria-valuenow={Math.round(shown)}
        aria-valuetext={formatTime(shown)}
        aria-readonly={!canSeek || undefined}
        className="relative flex h-target w-full touch-none select-none items-center"
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={onPointerUp}
        onPointerCancel={onPointerCancel}
        onKeyDown={onKeyDown}
      >
        {scrubMs !== null ? (
          <div
            data-testid="cue-bubble"
            className="pointer-events-none absolute -top-6 -translate-x-1/2 rounded-pill bg-overlay px-2 py-half text-micro text-primary numeric"
            style={{ left: `${pct}%` }}
          >
            {formatTime(scrubMs)}
          </div>
        ) : null}
        <div
          ref={trackRef}
          className={`relative w-full rounded-pill bg-stroke transition-[height] duration-scrub ease-out ${scrubMs !== null ? "h-slider-track-active" : "h-slider-track"}`}
        >
          <div
            className={`absolute inset-y-0 left-0 rounded-pill ${smooth ? "transition-[width] duration-[250ms] ease-linear" : ""}`}
            style={{ width: `${pct}%`, background: "var(--scrub-fill)" }}
          />
          {canSeek ? (
            <div
              data-testid="scrub-thumb"
              className={`absolute top-1/2 -translate-x-1/2 -translate-y-1/2 rounded-pill bg-primary transition-transform duration-scrub ${scrubMs !== null ? "h-scrub-thumb-active w-scrub-thumb-active" : "h-scrub-thumb w-scrub-thumb"}`}
              style={{ left: `${pct}%` }}
              aria-hidden="true"
            />
          ) : null}
        </div>
      </div>
      <div className="flex justify-between text-micro text-tertiary numeric">
        <span data-testid="elapsed">{formatTime(shown)}</span>
        <span data-testid="total">{formatTime(durationMs)}</span>
      </div>
      <span className="sr-only" aria-live="polite">
        {announce}
      </span>
    </div>
  );
}
