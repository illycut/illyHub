import { zoneDotsLabel, type ZoneDotModel } from "@/lib/selectors";

/**
 * Zone dot cluster (design system §6.3, §8): one dot per output, amber when live, dim when idle,
 * text-tertiary when the output is offline (never hidden), count chip past four live outputs.
 */
export function ZoneDots({ model, label }: { model: ZoneDotModel; label?: string }) {
  const name = label ?? zoneDotsLabel(model);
  if (model.kind === "count") {
    return (
      <span className="rounded-pill bg-overlay px-2 py-1 text-micro text-secondary numeric" aria-label={name}>
        {model.count} rooms
      </span>
    );
  }
  return (
    <span className="inline-flex items-center gap-1" role="img" aria-label={name}>
      {model.dots.map((d, i) => (
        <span
          key={i}
          data-live={d.live ? "true" : "false"}
          data-online={d.online ? "true" : "false"}
          className={`block h-2 w-2 rounded-pill ${!d.online ? "bg-tertiary" : d.live ? "bg-signal" : "bg-signal-dim"}`}
        />
      ))}
    </span>
  );
}
