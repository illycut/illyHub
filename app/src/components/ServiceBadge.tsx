import type { Source } from "@/lib/hub/types";

/**
 * Service identity chip (design system §2.4, §5.1). Production ships official brand-kit marks
 * from `public/brands/` (see `public/brands/LICENSES.md`, ai-dev #53). Until those are sourced,
 * a simplified stand-in glyph sits in the chip. The mark fills 68–72% of the chip. Never a button.
 */
export const OFFICIAL = false;

const META: Record<string, { label: string; mark: string; bg: string; fg: string; file: string }> = {
  tidal: { label: "Tidal", mark: "T", bg: "bg-badge-tidal", fg: "text-badge-tidal-fg", file: "/brands/tidal.svg" },
  ytmusic: { label: "YouTube Music", mark: "▶", bg: "bg-badge-ytmusic", fg: "text-badge-ytmusic-fg", file: "/brands/ytmusic.svg" },
  pandora: { label: "Pandora", mark: "P", bg: "bg-badge-pandora", fg: "text-badge-pandora-fg", file: "/brands/pandora.svg" },
};

export function serviceLabel(source: Source | null | undefined): string | null {
  if (!source) return null;
  return META[source]?.label ?? source;
}

export function ServiceBadge({
  source,
  size = 22,
  withLabel = false,
}: {
  source: Source | null | undefined;
  size?: 22 | 28;
  withLabel?: boolean;
}) {
  if (!source) return null;
  const m = META[source] ?? { label: source, mark: source.slice(0, 1).toUpperCase(), bg: "bg-overlay", fg: "text-secondary", file: "" };
  const markSize = Math.round(size * 0.7);
  const chip = (
    <span
      className={`inline-flex shrink-0 items-center justify-center rounded-pill ${m.bg} ${m.fg} font-bold`}
      style={{ width: size, height: size, fontSize: Math.round(size * 0.5) }}
      aria-hidden={withLabel}
      data-testid="service-chip"
    >
      {OFFICIAL && m.file ? (
        // eslint-disable-next-line @next/next/no-img-element
        <img src={m.file} alt="" width={markSize} height={markSize} draggable={false} />
      ) : (
        m.mark
      )}
    </span>
  );
  if (!withLabel) {
    return (
      <span className="inline-flex" role="img" aria-label={m.label}>
        {chip}
      </span>
    );
  }
  return (
    <span className={`inline-flex items-center gap-2 rounded-pill bg-raised pl-1 pr-3 py-1 text-caption text-secondary`}>
      {chip}
      <span>{m.label}</span>
    </span>
  );
}
