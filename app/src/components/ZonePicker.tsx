"use client";
import { useEffect, useMemo, useState } from "react";
import { useShallow } from "zustand/react/shallow";
import { Sheet } from "./Sheet";
import { AmpIcon, CheckIcon, PowerIcon, SpeakerIcon } from "./icons";
import { useHub } from "@/lib/hub/store";
import { resolveActiveSide, zoneRowsForState } from "@/lib/selectors";
import { readLastTarget, rememberTargets } from "@/lib/prefs";
import { refKey } from "@/lib/hub/library";
import type { PlayRequest } from "@/lib/ui/chrome";
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

/** Why a side cannot play the requested content, or null when it can. */
export function unavailableReason(side: Side, play: PlayRequest | null | undefined): string | null {
  if (!play?.availability) return null;
  const ok = side.vendor === "heos" ? play.availability.heos : play.availability.sonos;
  return ok ? null : `Not available on ${side.vendor === "heos" ? "HEOS" : "Sonos"}`;
}

/**
 * Pre-highlight rule (PRD Decision 4): history last targets first, localStorage per content ref
 * second, and on first use the side Now Playing is showing (or the first playing side), so the
 * common case is always two taps.
 */
export function initialSelection(play: PlayRequest, state: HubState | null, activeSideId: string | null): string[] {
  const sideIds = Object.keys(state?.sides ?? {});
  const usable = (id: string) => sideIds.includes(id) && !unavailableReason(state!.sides[id]!, play);
  const fromHistory = play.preferred.filter(usable);
  if (fromHistory.length) return fromHistory;
  const remembered = readLastTarget(refKey(play.content_ref))?.filter(usable) ?? [];
  if (remembered.length) return remembered;
  const active = resolveActiveSide(state, activeSideId);
  return active && usable(active.id) ? [active.id] : [];
}

export function playButtonLabel(names: string[]): string {
  if (names.length === 0) return "Choose a room";
  if (names.length === 1) return `Play on ${names[0]}`;
  if (names.length === 2) return `Play on ${names[0]} and ${names[1]}`;
  return `Play in ${names.length} rooms`;
}

/**
 * Zone / target picker (design system §6.6): one row per output, amp glyph for HEOS, speaker
 * for Sonos. HEOS rows carry a labelled power toggle for their Denon zone; Sonos rows show
 * Playing/Idle. Multi-select with amber checks feeds Sync Play (Phase 4).
 *
 * Two modes. Default: choosing a row makes it the Now Playing side and a single selection
 * closes. Play mode (`play` set): the sheet opens with the last-used targets pre-highlighted,
 * rows toggle, and the primary button confirms. The common case is two fast taps (Decision 4).
 * There is one instance, mounted by PlayerChrome above the Now Playing overlay.
 */
export function ZonePicker({
  open,
  onClose,
  play = null,
  onConfirm,
}: {
  open: boolean;
  onClose: () => void;
  play?: PlayRequest | null;
  onConfirm?: (targets: string[]) => void;
}) {
  const rows = useHub((s) => zoneRowsForState(s.state));
  const sideIds = useHub(useShallow((s) => Object.keys(s.state?.sides ?? {})));
  const selected = useHub((s) => s.selectedTargets);
  const setTargets = useHub((s) => s.setTargets);
  const selectSide = useHub((s) => s.selectSide);
  const zonePower = useHub((s) => s.zonePower);
  // Play-mode selection: derived from the request until the user toggles a row; the override is
  // keyed to the request so a new request starts fresh.
  const [override, setOverride] = useState<{ req: PlayRequest; sel: string[] } | null>(null);
  const initial = useMemo(() => {
    if (!play) return [];
    const hub = useHub.getState();
    return initialSelection(play, hub.state, hub.activeSideId);
    // sideIds is the stable proxy for "the set of sides changed"
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [play, sideIds]);
  const playSel = play && override?.req === play ? override.sel : initial;

  const playMode = !!play;

  useEffect(() => {
    if (!open || play || selected.length > 0) return;
    const remembered = readLastTarget()?.filter((id) => sideIds.includes(id));
    if (remembered && remembered.length) setTargets(remembered);
  }, [open, play, selected.length, sideIds, setTargets]);

  const choose = (id: string) => {
    if (playMode && play) {
      const next = playSel.includes(id) ? playSel.filter((t) => t !== id) : [...playSel, id];
      setOverride({ req: play, sel: next });
      return;
    }
    const wasSelected = selected.includes(id);
    const next = wasSelected ? selected.filter((t) => t !== id) : [...selected, id];
    setTargets(next);
    rememberTargets(next);
    if (!wasSelected) {
      selectSide(id);
      if (next.length === 1) onClose();
    }
  };

  const confirm = () => {
    if (!play || playSel.length === 0) return;
    rememberTargets(playSel, refKey(play.content_ref));
    setTargets(playSel);
    selectSide(playSel[0]!);
    onConfirm?.(playSel);
    onClose();
  };

  const current = playMode ? playSel : selected;
  const names = rows.filter((r) => current.includes(r.side.id)).map((r) => r.side.name);

  return (
    <Sheet open={open} onClose={onClose} title={playMode ? `Play “${play!.title}” on` : "Play on"} testId="zone-picker">
      <ul className="flex flex-col">
        {rows.map(({ side, zones, coordinatorOnline }) => {
          const on = current.includes(side.id);
          const Glyph = side.vendor === "heos" ? AmpIcon : SpeakerIcon;
          const status = sideStatus(side, coordinatorOnline, zones);
          // Disabled rows keep full opacity: name in text-secondary, reason in text-tertiary (UX U12).
          const reason = !coordinatorOnline ? "Offline" : unavailableReason(side, play);
          const tone = status.tone === "tertiary" ? "text-tertiary" : status.tone === "signal" ? "text-signal" : "text-secondary";
          return (
            <li key={side.id} className="flex min-h-row items-center gap-3 border-b border-stroke last:border-0" data-testid={`zone-row-${side.id}`}>
              <button
                type="button"
                className="flex min-h-row flex-1 items-center gap-3 rounded-control text-left"
                aria-pressed={on}
                aria-disabled={!!reason || undefined}
                disabled={!!reason}
                onClick={() => choose(side.id)}
              >
                <span
                  className={`flex h-6 w-6 items-center justify-center rounded-pill border ${on ? "border-signal bg-signal text-base" : "border-stroke text-transparent"}`}
                  aria-hidden="true"
                >
                  <CheckIcon size={16} />
                </span>
                <span className={reason ? "text-tertiary" : "text-secondary"} aria-hidden="true">
                  <Glyph size={22} />
                </span>
                <span className="flex-1">
                  <span className={`block text-body ${reason ? "text-secondary" : "text-primary"}`}>{side.name}</span>
                  <span className={`block text-micro ${reason ? "text-tertiary" : tone}`}>
                    {reason ?? status.text}
                    {!reason && side.member_ids.length > 1 ? ` · ${side.member_ids.length} speakers` : ""}
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
      {playMode ? (
        <div className="pt-4">
          <button
            type="button"
            className="flex h-target w-full items-center justify-center rounded-control bg-overlay text-body text-primary disabled:opacity-40"
            disabled={playSel.length === 0}
            onClick={confirm}
            data-testid="confirm-play"
          >
            {playButtonLabel(names)}
          </button>
        </div>
      ) : null}
    </Sheet>
  );
}

export type { HubState };
