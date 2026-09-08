/** Small color helpers for the contrast-guarded scrubber fill (design system §2.2, §9). */
export function hexToRgb(hex: string): [number, number, number] | null {
  const m = /^#?([0-9a-f]{6})$/i.exec(hex.trim());
  if (!m) return null;
  const n = parseInt(m[1]!, 16);
  return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
}

export function relativeLuminance([r, g, b]: [number, number, number]): number {
  const lin = (c: number) => {
    const s = c / 255;
    return s <= 0.03928 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4;
  };
  return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b);
}

export function contrastRatio(a: string, b: string): number {
  const ra = hexToRgb(a);
  const rb = hexToRgb(b);
  if (!ra || !rb) return 1;
  const la = relativeLuminance(ra);
  const lb = relativeLuminance(rb);
  const [hi, lo] = la > lb ? [la, lb] : [lb, la];
  return (hi + 0.05) / (lo + 0.05);
}

export const STROKE_HEX = "#2e2e38";
export const TEXT_PRIMARY_HEX = "#f2f2f5";

/**
 * Scrubber fill: the art accent when the hub says it is safe AND it clears 3:1 against the track
 * (`stroke`); otherwise text-primary. bg-raised is darker than the track, so it is never a fill.
 */
export function scrubFill(accent: string | null | undefined, accentIsSafe: boolean): string {
  if (accent && accentIsSafe && contrastRatio(accent, STROKE_HEX) >= 3) return accent;
  return "var(--text-primary)";
}
