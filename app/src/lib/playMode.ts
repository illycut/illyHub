/**
 * Shuffle / repeat (ai-dev #79, docs/api.md "Play mode"). Pure so the toggles, the store and the
 * tests agree. Facts baked in: `Side.play_mode` is `{shuffle, repeat: off|one|all}`; the hub
 * refuses changes with `sync_active` while a Sync Play session is live (it forces shuffle off on
 * both sides at start), and radio sources (stations) have no modes at all.
 */
import messages from "./hub/messages.json";
import { asPlayMode, type PlayMode, type RepeatMode } from "./hub/library";
import type { NowPlaying, Side } from "./hub/types";

/** One sentence for the Sync Play lock; the hub adopts it (`templates.hub.play_mode_locked`) and wins once regenerated. */
export const PLAY_MODES_OFF_DURING_SYNC: string = (messages as { templates?: { hub?: { play_mode_locked?: string } } }).templates?.hub?.play_mode_locked ?? "Shuffle and repeat are off during Sync Play.";

/** The side's play mode (generated `Side.play_mode`); a hub that does not report one reads as both off. */
export function playModeOf(side: Pick<Side, "play_mode"> | null | undefined): PlayMode {
  return asPlayMode(side?.play_mode);
}

/** Repeat cycles off → all → one → off (one tap per step, §6.4 transport conventions). */
export function nextRepeat(cur: RepeatMode): RepeatMode {
  return cur === "off" ? "all" : cur === "all" ? "one" : "off";
}

/** Accessible names (U11): the toggle is "Shuffle" / "Repeat" with `aria-pressed`; repeat adds the mode word when on. */
export function repeatLabel(mode: RepeatMode): string {
  return mode === "off" ? "Repeat" : `Repeat ${mode}`;
}

/** The accessible name is the same on and off; `aria-pressed` carries the state (U11). */
export function shuffleLabel(): string {
  return "Shuffle";
}

/** Stations have no queue, so no shuffle or repeat: hide the toggles rather than disable them. */
export function playModesHidden(np: NowPlaying | null | undefined): boolean {
  return np?.content_ref?.kind === "station" || np?.source === "pandora";
}
