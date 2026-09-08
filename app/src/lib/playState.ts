import type { PlayState, Side } from "./hub/types";

/**
 * The one "is it playing" predicate: `play` and `buffering` (a play the device has accepted but
 * not yet voiced) both count. Every surface — dots, active-side resolution, the play/pause icon,
 * the scrubber tick, position interpolation, sync and Pandora rules — reads this, never
 * `play_state === "play"`. Lives in its own module so selectors, sync and pandora can all import it
 * without a cycle.
 */
export function isPlayingState(s: PlayState | null | undefined): boolean {
  return s === "play" || s === "buffering";
}

export function isPlaying(side: Pick<Side, "play_state"> | null | undefined): boolean {
  return isPlayingState(side?.play_state);
}

/** Definitive states the hub reports once a command has landed; `buffering`/`unknown` are transitional. */
export function isSettledState(s: PlayState | null | undefined): boolean {
  return s === "play" || s === "pause" || s === "stop";
}
