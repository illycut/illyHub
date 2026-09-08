"use client";
import { ListIcon, RepeatIcon, RepeatOneIcon, ShuffleIcon } from "./icons";
import type { PlayMode } from "@/lib/hub/library";
import { PLAY_MODES_OFF_DURING_SYNC, nextRepeat, repeatLabel, shuffleLabel } from "@/lib/playMode";
import { UP_NEXT } from "@/lib/queue";

/** On-state marker under a toggle's glyph: shape as well as fill (§9), never color alone. */
function Dot({ on }: { on: boolean }) {
  return <span className={`mt-half block h-1 w-1 rounded-pill ${on ? "bg-primary" : "bg-transparent"}`} aria-hidden="true" data-on={on ? "true" : undefined} />;
}

/**
 * Playback options under the transport row (Phase 8; ai-dev #77, #79): shuffle toggle, the plain
 * text "Up next" button, repeat toggle (off → all → one). 48px targets, 8px gaps, glyph +
 * `aria-pressed` + label so state never rides on color alone (§9). Toggles disable with one caption
 * during Sync Play (the hub forces shuffle off and refuses changes); they are hidden for stations
 * (`modesHidden`), which have no queue; the Up next button stays so the sheet can say so.
 *
 * Why a plain text button rather than a swipe-up: the Now Playing layer already owns the drag-down
 * gesture and the sheets own drag-to-dismiss, so a third vertical gesture on the meta area would
 * collide with both; a visible, labelled button is also the only discoverable form for assistive
 * tech and keyboards.
 */
export function PlayOptionsRow({
  mode,
  modesHidden,
  syncActive,
  disabled = false,
  onShuffle,
  onRepeat,
  onUpNext,
}: {
  mode: PlayMode;
  /** Radio source: no shuffle/repeat at all. */
  modesHidden: boolean;
  /** Sync Play live: toggles disabled with the caption. */
  syncActive: boolean;
  /** No side (nothing to act on). */
  disabled?: boolean;
  onShuffle: (on: boolean) => void;
  onRepeat: (repeat: PlayMode["repeat"]) => void;
  onUpNext: () => void;
}) {
  const RepeatGlyph = mode.repeat === "one" ? RepeatOneIcon : RepeatIcon;
  // On-state carries shape as well as fill (§9): a small dot under the glyph, never color alone.
  const toggleCls = (on: boolean) =>
    `hit-target relative flex flex-col items-center justify-center rounded-control transition-transform duration-press active:scale-[0.97] disabled:active:scale-100 ${on ? "bg-overlay text-primary" : "text-secondary"} disabled:text-tertiary`;
  const captionId = "play-modes-caption";
  return (
    <div className="mx-auto flex w-full max-w-transport flex-col items-center gap-1" data-testid="play-options" data-np-options>
      <div className="flex w-full items-center justify-between gap-gap-min">
        {modesHidden ? (
          <span className="w-target" aria-hidden="true" />
        ) : (
          <button
            type="button"
            className={toggleCls(mode.shuffle)}
            aria-label={shuffleLabel()}
            aria-pressed={mode.shuffle}
            aria-describedby={syncActive ? captionId : undefined}
            disabled={disabled || syncActive}
            onClick={() => onShuffle(!mode.shuffle)}
            data-testid="shuffle-toggle"
          >
            <ShuffleIcon size={22} />
            <Dot on={mode.shuffle} />
          </button>
        )}
        <button
          type="button"
          className="flex min-h-target items-center gap-2 rounded-control px-3 text-caption text-secondary disabled:text-tertiary"
          onClick={onUpNext}
          disabled={disabled}
          data-testid="open-queue"
        >
          <span aria-hidden="true">
            <ListIcon size={18} />
          </span>
          {UP_NEXT}
        </button>
        {modesHidden ? (
          <span className="w-target" aria-hidden="true" />
        ) : (
          <button
            type="button"
            className={toggleCls(mode.repeat !== "off")}
            aria-label={repeatLabel(mode.repeat)}
            aria-pressed={mode.repeat !== "off"}
            aria-describedby={syncActive ? captionId : undefined}
            disabled={disabled || syncActive}
            onClick={() => onRepeat(nextRepeat(mode.repeat))}
            data-testid="repeat-toggle"
            data-repeat={mode.repeat}
          >
            <RepeatGlyph size={22} />
            <Dot on={mode.repeat !== "off"} />
          </button>
        )}
      </div>
      {/* The caption slot always holds one micro line so play/pause never moves when Sync Play starts (U3). */}
      <p id={captionId} className="min-h-[var(--type-micro-line)] text-micro text-secondary" data-testid="play-modes-caption" aria-hidden={syncActive && !modesHidden ? undefined : "true"}>
        {syncActive && !modesHidden ? PLAY_MODES_OFF_DURING_SYNC : ""}
      </p>
    </div>
  );
}
