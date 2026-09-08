import type { PlayState, Position } from "./hub/types";
import { isPlayingState } from "./playState";

/**
 * Hub positions are estimates stamped with `reported_at`. While playing, extrapolate forward from
 * that stamp; clamp to the track duration when known (docs/api.md, PRD NP-3).
 */
export function interpolatePosition(
  pos: Position | null | undefined,
  playState: PlayState | undefined,
  nowMs: number,
  durationMs?: number | null,
): number {
  if (!pos) return 0;
  let ms = pos.position_ms;
  if (isPlayingState(playState)) {
    const reported = Date.parse(pos.reported_at);
    if (Number.isFinite(reported)) ms += Math.max(0, nowMs - reported);
  }
  if (durationMs != null && durationMs > 0) ms = Math.min(ms, durationMs);
  return Math.max(0, Math.round(ms));
}

/** Whether the scrubber should animate smoothly or snap: low confidence means snap. */
export function shouldAnimate(pos: Position | null | undefined): boolean {
  return (pos?.confidence ?? 0) >= 0.9;
}
