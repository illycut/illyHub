"use client";
import { useEffect } from "react";
import { useShallow } from "zustand/react/shallow";
import { Sheet } from "./Sheet";
import { AmpIcon, CheckIcon, PowerIcon, SpeakerIcon } from "./icons";
import { useHub } from "@/lib/hub/store";
import { zoneRowsForState } from "@/lib/selectors";
import { readLastTarget, writeLastTarget } from "@/lib/prefs";
import type { HubState, Side, Zone } from "@/lib/hub/types";

/** Sentence-case status copy (design system §10). Offline is the only tertiary state. */
export function sideStatus(side: Side, coordinatorOnline: boolean, zones: Zone[]): { text: string; tone: "secondary" | "tertiary" | "signal" } {
  if (!coordinatorOnline) return { text: "Offline", tone: "tertiary" };
  if (side.play_state === "play") return { text: "Playing", tone: "signal" };
  if (side.play_state === "pause") return { text: "Paused", tone: "secondary" };
  if (side.vendor === "sonos") return { text: "Idle", tone: "secondary" };
  const z = zones[0];
  if (z) return { text: z.power ? "On" : "Off", tone: "secondary" };
  return { text: "Idle", tone: "secondary" };
}

/**
 * Zone / target picker (design system §6.6): one row per output, amp glyph for HEOS, speaker
 * for Sonos. HEOS rows carry a labelled power toggle for their Denon zone; Sonos rows show
 * Playing/Idle. Multi-select with amber checks feeds Sync Play (Phase 4). Last-used targets are
 * remembered. Selecting a row makes it the Now Playing side; choosing the only selection closes.
 */
export function ZonePicker({ open, onClose }: { open: boolean; onClose: () => void }) {
  const rows = useHub((s) => zoneRowsForState(s.state));
  const sideIds = useHub(useShallow((s) => Object.keys(s.state?.sides ?? {})));
  const selected = useHub((s) => s.selectedTargets);
  const setTargets = useHub((s) => s.setTargets);
  const selectSide = useHub((s) => s.selectSide);
  const zonePower = useHub((s) => s.zonePower);

  useEffect(() => {
    if (!open || selected.length > 0) return;
    const remembered = readLastTarget()?.filter((id) => sideIds.includes(id));
    if (remembered && remembered.length) setTargets(remembered);
  }, [open, selected.length, setTargets, sideIds]);

  const choose = (id: string) => {
    const wasSelected = selected.includes(id);
    const next = wasSelected ? selected.filter((t) => t !== id) : [...selected, id];
    setTargets(next);
    writeLastTarget(next);
    if (!wasSelected) {
      selectSide(id);
      if (next.length === 1) onClose();
    }
  };

  return (
    <Sheet open={open} onClose={onClose} title="Play on" testId="zone-picker">
      <ul className="flex flex-col">
        {rows.map(({ side, zones, coordinatorOnline }) => {
          const on = selected.includes(side.id);
          const Glyph = side.vendor === "heos" ? AmpIcon : SpeakerIcon;
          const status = sideStatus(side, coordinatorOnline, zones);
          const tone = status.tone === "tertiary" ? "text-tertiary" : status.tone === "signal" ? "text-signal" : "text-secondary";
          return (
            <li key={side.id} className={`flex min-h-row items-center gap-3 border-b border-stroke last:border-0 ${!coordinatorOnline ? "text-tertiary" : ""}`} data-testid={`zone-row-${side.id}`}>
              <button
                type="button"
                className="flex min-h-row flex-1 items-center gap-3 rounded-control text-left"
                aria-pressed={on}
                onClick={() => choose(side.id)}
              >
                <span
                  className={`flex h-6 w-6 items-center justify-center rounded-pill border ${on ? "border-signal bg-signal text-base" : "border-stroke text-transparent"}`}
                  aria-hidden="true"
                >
                  <CheckIcon size={16} />
                </span>
                <span className={!coordinatorOnline ? "text-tertiary" : "text-secondary"} aria-hidden="true">
                  <Glyph size={22} />
                </span>
                <span className="flex-1">
                  <span className="block text-body text-primary">{side.name}</span>
                  <span className={`block text-micro ${tone}`}>
                    {status.text}
                    {side.member_ids.length > 1 ? ` · ${side.member_ids.length} speakers` : ""}
                  </span>
                </span>
              </button>
              {zones.map((z) => (
                <button
                  key={z.id}
                  type="button"
                  className={`hit-target flex flex-col items-center justify-center gap-half rounded-control ${z.power ? "text-signal" : "text-tertiary"}`}
                  aria-label={`${z.name} power`}
                  aria-pressed={z.power}
                  onClick={() => void zonePower(z.id, !z.power)}
                  data-testid={`power-${z.id}`}
                >
                  <span className={`flex h-7 w-7 items-center justify-center rounded-pill border-2 ${z.power ? "border-signal bg-signal/20" : "border-stroke"}`}>
                    <PowerIcon size={16} />
                  </span>
                  <span className="text-micro">{z.key === "main" ? "Main" : z.name}</span>
                </button>
              ))}
            </li>
          );
        })}
      </ul>
      {rows.length === 0 ? <p className="py-4 text-body text-secondary">No rooms yet. The hub is still discovering players.</p> : null}
    </Sheet>
  );
}

export type { HubState };
