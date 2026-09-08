/** Scrubber math: pixel ↔ ms with clamping. Pure so it is unit-testable. */
export function pxToMs(clientX: number, left: number, width: number, durationMs: number): number {
  if (width <= 0 || durationMs <= 0) return 0;
  const t = Math.min(1, Math.max(0, (clientX - left) / width));
  return Math.round(t * durationMs);
}

export function msToPct(ms: number, durationMs: number | null | undefined): number {
  if (!durationMs || durationMs <= 0) return 0;
  return Math.min(100, Math.max(0, (ms / durationMs) * 100));
}
