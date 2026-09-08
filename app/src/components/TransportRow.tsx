"use client";
import { NextIcon, PauseIcon, PlayIcon, PrevIcon, SkipBack15Icon, SkipForward15Icon } from "./icons";
import type { PlayState } from "@/lib/hub/types";
import { isPlayingState } from "@/lib/playState";

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
  outlined = false,
  describedBy,
  onPrev,
  onBack15,
  onToggle,
  onForward15,
  onNext,
}: {
  playState: PlayState | undefined;
  caps: TransportCaps;
  disabled?: boolean;
  /** Another party owns playback (Pandora Sync): render the primary disc outlined and unfilled. */
  outlined?: boolean;
  /** Id of the caption that explains a disabled row (e.g. Pandora Sync), announced with each control. */
  describedBy?: string;
  onPrev: () => void;
  onBack15: () => void;
  onToggle: () => void;
  onForward15: () => void;
  onNext: () => void;
}) {
  // Buffering counts as playing: the device accepted the play, so the icon offers pause.
  const playing = isPlayingState(playState);
  const desc = disabled ? describedBy : undefined;
  // While another party owns playback (Pandora Sync) an outlined, unfilled disc reads as "not
  // yours right now" rather than a faded button; plain disabled (no side yet) stays faded.
  const discCls = outlined ? "border border-stroke bg-transparent text-tertiary" : "bg-primary text-base disabled:opacity-40";
  return (
    <div className="mx-auto flex w-full max-w-transport items-center justify-between" data-testid="transport">
      <Btn label="Previous track" disabled={disabled || !caps.supports_prev} onClick={onPrev} describedBy={desc}>
        <PrevIcon size={28} />
      </Btn>
      <Btn label="Back 15 seconds" disabled={disabled || !caps.supports_seek} onClick={onBack15} describedBy={desc}>
        <SkipBack15Icon size={28} />
      </Btn>
      <button
        type="button"
        aria-label={playing ? "Pause" : "Play"}
        aria-pressed={playing}
        disabled={disabled}
        aria-describedby={desc}
        onClick={onToggle}
        className={`flex h-play w-play items-center justify-center rounded-pill transition-transform duration-press active:scale-[0.97] ${discCls}`}
        data-testid="play-toggle"
        data-outlined={outlined ? "true" : undefined}
      >
        {playing ? <PauseIcon size={30} /> : <PlayIcon size={30} />}
      </button>
      <Btn label="Forward 15 seconds" disabled={disabled || !caps.supports_seek} onClick={onForward15} describedBy={desc}>
        <SkipForward15Icon size={28} />
      </Btn>
      <Btn label="Next track" disabled={disabled || !caps.supports_next} onClick={onNext} describedBy={desc}>
        <NextIcon size={28} />
      </Btn>
    </div>
  );
}

function Btn({ label, disabled, onClick, describedBy, children }: { label: string; disabled?: boolean; onClick: () => void; describedBy?: string; children: React.ReactNode }) {
  return (
    <button
      type="button"
      aria-label={label}
      disabled={disabled}
      aria-describedby={describedBy}
      onClick={onClick}
      className="flex h-target-lg w-target-lg items-center justify-center rounded-control text-primary transition-transform duration-press active:scale-[0.97] disabled:opacity-40 disabled:active:scale-100"
    >
      {children}
    </button>
  );
}
