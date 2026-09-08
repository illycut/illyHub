import type { PlayState, Side } from "./hub/types";

/**
 * The one "is it playing" predicate. The contract's play states are play / pause / stop / unknown;
 * the hub holds the last known state across a device's transient, so only `play` is live. Every
 * surface — dots, active-side resolution, the play/pause icon, the scrubber tick, position
 * interpolation, sync and Pandora rules — reads this, never `play_state === "play"` inline. Lives
 * in its own module so selectors, sync and pandora can all import it without a cycle.
 */
export function isPlayingState(s: PlayState | null | undefined): boolean {
  return s === "play";
}

export function isPlaying(side: Pick<Side, "play_state"> | null | undefined): boolean {
  return isPlayingState(side?.play_state);
}

/** States the hub reports once a command has landed; `unknown` is transitional and never settles a held intent. */
export function isSettledState(s: PlayState | null | undefined): boolean {
  return s === "play" || s === "pause" || s === "stop";
}
