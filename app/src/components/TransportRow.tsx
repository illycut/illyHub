"use client";
import { NextIcon, PauseIcon, PlayIcon, PrevIcon, SkipBack15Icon, SkipForward15Icon } from "./icons";
import type { PlayState } from "@/lib/hub/types";

export interface TransportCaps {
  supports_seek: boolean;
  supports_next: boolean;
  supports_prev: boolean;
}

/**
 * Five-button transport row (design system §6.4, PRD Decision 3): 56px hit targets, row capped at
 * 372px and space-distributed, 64px primary. ±15 disabled when the side cannot seek; next/prev
 * disabled when the source cannot; everything disabled with no side.
 */
export function TransportRow({
  playState,
  caps,
  disabled = false,
  onPrev,
  onBack15,
  onToggle,
  onForward15,
  onNext,
}: {
  playState: PlayState | undefined;
  caps: TransportCaps;
  disabled?: boolean;
  onPrev: () => void;
  onBack15: () => void;
  onToggle: () => void;
  onForward15: () => void;
  onNext: () => void;
}) {
  const playing = playState === "play";
  return (
    <div className="mx-auto flex w-full max-w-transport items-center justify-between" data-testid="transport">
      <Btn label="Previous track" disabled={disabled || !caps.supports_prev} onClick={onPrev}>
        <PrevIcon size={28} />
      </Btn>
      <Btn label="Back 15 seconds" disabled={disabled || !caps.supports_seek} onClick={onBack15}>
        <SkipBack15Icon size={28} />
      </Btn>
      <button
        type="button"
        aria-label={playing ? "Pause" : "Play"}
        aria-pressed={playing}
        disabled={disabled}
        onClick={onToggle}
        className="flex h-play w-play items-center justify-center rounded-pill bg-primary text-base transition-transform duration-press active:scale-[0.97] disabled:opacity-40"
        data-testid="play-toggle"
      >
        {playing ? <PauseIcon size={30} /> : <PlayIcon size={30} />}
      </button>
      <Btn label="Forward 15 seconds" disabled={disabled || !caps.supports_seek} onClick={onForward15}>
        <SkipForward15Icon size={28} />
      </Btn>
      <Btn label="Next track" disabled={disabled || !caps.supports_next} onClick={onNext}>
        <NextIcon size={28} />
      </Btn>
    </div>
  );
}

function Btn({ label, disabled, onClick, children }: { label: string; disabled?: boolean; onClick: () => void; children: React.ReactNode }) {
  return (
    <button
      type="button"
      aria-label={label}
      disabled={disabled}
      onClick={onClick}
      className="flex h-target-lg w-target-lg items-center justify-center rounded-control text-primary transition-transform duration-press active:scale-[0.97] disabled:opacity-40 disabled:active:scale-100"
    >
      {children}
    </button>
  );
}
