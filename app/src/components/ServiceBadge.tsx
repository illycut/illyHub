import type { Source } from "@/lib/hub/types";

/**
 * Service identity chip (design system §2.4, §5.1). Production ships official brand-kit marks
 * (ai-dev #53); until then a simplified stand-in letter mark sits in the chip. Never a button.
 */
const META: Record<string, { label: string; mark: string; bg: string; fg: string }> = {
  tidal: { label: "Tidal", mark: "T", bg: "bg-badge-tidal", fg: "text-badge-tidal-fg" },
  ytmusic: { label: "YouTube Music", mark: "▶", bg: "bg-badge-ytmusic", fg: "text-badge-ytmusic-fg" },
  pandora: { label: "Pandora", mark: "P", bg: "bg-badge-pandora", fg: "text-badge-pandora-fg" },
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
  const m = META[source] ?? { label: source, mark: source.slice(0, 1).toUpperCase(), bg: "bg-overlay", fg: "text-secondary" };
  const chip = (
    <span
      className={`inline-flex items-center justify-center rounded-pill ${m.bg} ${m.fg} font-bold`}
      style={{ width: size, height: size, fontSize: Math.round(size * 0.5) }}
      aria-hidden={withLabel}
    >
      {m.mark}
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
