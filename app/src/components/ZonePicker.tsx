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
import { SYNC_BUTTON, syncButtonLabel, syncEligibility, syncNote, type Eligibility } from "@/lib/sync";
import { pandoraConcurrentNote, unavailableCopy } from "@/lib/pandora";
import { PANDORA_SYNC_BUTTON, PANDORA_SYNC, pandoraSyncNote, pandoraSyncOffered } from "@/lib/airplay";
import { isSyncActive } from "@/lib/sync";
import { useLibrary } from "@/lib/library/store";

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
 * Why a side cannot play the requested content, or null when it can. The sentence is the hub's
 * (`availability.reasons[vendor]`, via the exported templates); without a reason the request's
 * per-vendor link state decides (see `unavailableCopy`).
 */
export function unavailableReason(side: Side, play: PlayRequest | null | undefined): string | null {
  if (!play?.availability) return null;
  const ok = side.vendor === "heos" ? play.availability.heos : play.availability.sonos;
  return ok ? null : unavailableCopy(play.content_ref?.service, side.vendor, { availability: play.availability, unlinked: play.unlinked_vendors });
}

/**
 * Pre-highlight rule (PRD Decision 4): history last targets first, localStorage per content ref
 * second, then the side Now Playing is showing (or the first playing side), and finally, when only
 * one room can play the content at all, that room (UX U2), so the common case is always two taps.
 */
export function initialSelection(play: PlayRequest, state: HubState | null, activeSideId: string | null): string[] {
  const sideIds = Object.keys(state?.sides ?? {});
  const online = (id: string) => state?.players[state.sides[id]!.coordinator_player_id]?.online ?? true;
  const usable = (id: string) => sideIds.includes(id) && online(id) && !unavailableReason(state!.sides[id]!, play);
  const fromHistory = play.preferred.filter(usable);
  if (fromHistory.length) return fromHistory;
  const remembered = readLastTarget(refKey(play.content_ref))?.filter(usable) ?? [];
  if (remembered.length) return remembered;
  const active = resolveActiveSide(state, activeSideId);
  if (active && usable(active.id)) return [active.id];
  const usableIds = sideIds.filter(usable);
  return usableIds.length === 1 ? usableIds : [];
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
  onPandoraSync,
}: {
  open: boolean;
  onClose: () => void;
  play?: PlayRequest | null;
  /** Play mode: `mode` is "sync" when the button was Sync Play (one HEOS + one Sonos side, Tidal content). */
  onConfirm?: (targets: string[], mode: Eligibility) => void;
  /** Phase 7 (experimental): a Pandora station handed to the hub Mac's AirPlay bridge for the selected rooms. */
  onPandoraSync?: (targets: string[]) => void;
}) {
  // The bridge capability comes from Settings (hub.airplay); the button shows only when it is available,
  // never while a Sync Play session is live; and Sync Play is hidden while Pandora Sync is on (S10).
  const airplay = useLibrary((s) => s.settings.data?.hub.airplay);
  const syncActive = useHub((s) => isSyncActive(s.state?.sync));
  const pandoraActive = useHub((s) => !!s.state?.pandora_sync?.active);
  const rows = useHub((s) => zoneRowsForState(s.state));
  const sides = useHub((s) => s.state?.sides);
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

  const eligibility: Eligibility = useMemo(
    () => (play ? syncEligibility(playSel, sides ?? {}, play.content_ref) : { mode: "play", syncReason: null }),
    [play, playSel, sides],
  );

  // Pandora single-stream warning (PRD §3.5): a string selector, so 1 Hz position deltas do not
  // re-render the sheet; it changes only when a room starts or stops playing Pandora.
  const pandoraNote = useHub((s) => (playMode ? pandoraConcurrentNote(play?.content_ref.service, playSel, s.state) : null));

  const confirm = (mode: Eligibility = eligibility) => {
    if (!play || playSel.length === 0) return;
    rememberTargets(playSel, refKey(play.content_ref));
    setTargets(playSel);
    // Sync Play shows the master (HEOS) in Now Playing; a plain play shows the first target.
    selectSide(mode.mode === "sync" ? mode.heos[0]! : playSel[0]!);
    onConfirm?.(playSel, mode);
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
          // Disabled rows keep full opacity: name in text-secondary; an explanation of unavailability
          // reads in text-secondary (UX U8), "Offline" stays tertiary (§8).
          const reason = !coordinatorOnline ? "Offline" : unavailableReason(side, play);
          const reasonTone = reason === "Offline" ? "text-tertiary" : "text-secondary";
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
                  <span className={`block text-micro ${reason ? reasonTone : tone}`}>
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
      {pandoraNote ? (
        <p id="pandora-note" className="pt-3 text-micro text-secondary" data-testid="pandora-note">
          {pandoraNote}
        </p>
      ) : null}
      {playMode && eligibility.mode === "sync" && !pandoraActive ? (
        // Sync Play is the only amber-filled button in the app (design system §6.8): it creates a live audio state.
        <div className="flex flex-col items-center gap-2 pt-4">
          <button
            type="button"
            className="flex h-target w-full items-center justify-center rounded-control bg-signal text-body font-semibold text-base"
            onClick={() => confirm()}
            data-testid="confirm-play"
            data-mode="sync"
            aria-describedby={pandoraNote ? "sync-note pandora-note" : "sync-note"}
          >
            {syncButtonLabel(playSel.length)}
          </button>
          <p id="sync-note" className="text-center text-micro text-secondary" data-testid="sync-note">
            {syncNote(
              eligibility.heos.map((id) => rows.find((r) => r.side.id === id)?.side.name ?? "another room"),
              eligibility.sonos.map((id) => rows.find((r) => r.side.id === id)?.side.name ?? "another room"),
            )}
            {play!.note ? (
              <>
                <br />
                <span data-testid="sync-offer-note">{play!.note}</span>
              </>
            ) : null}
          </p>
          {/* Unsynced multi-room play stays one tap away: plain secondary button, 8px below the note. */}
          <button
            type="button"
            className="mt-gap-min flex min-h-target w-full items-center justify-center rounded-control text-body text-secondary"
            onClick={() => confirm({ mode: "play", syncReason: null })}
            data-testid="confirm-play-plain"
          >
            {playButtonLabel(names)}
          </button>
        </div>
      ) : playMode ? (
        <div className="flex flex-col gap-2 pt-4">
          {/* Footer order (UX U1): station note → confirm → Pandora Sync → its note. */}
          {eligibility.mode === "play" && eligibility.syncReason && play!.content_ref.kind === "station" ? (
            // Stations never Sync Play (Pandora picks per room): one caption, no dead button (design §13 1.3).
            <p className="text-center text-micro text-secondary" data-testid="station-sync-note">
              {eligibility.syncReason}
            </p>
          ) : null}
          {pandoraActive && eligibility.mode === "sync" ? (
            <p className="text-center text-micro text-secondary" data-testid="sync-hidden-note">
              {PANDORA_SYNC} is on. Stop it to use Sync Play.
            </p>
          ) : null}
          <button
            type="button"
            className="flex h-target w-full items-center justify-center rounded-control bg-overlay text-body text-primary disabled:opacity-40"
            disabled={playSel.length === 0}
            // A plain play even when the selection would qualify for Sync Play but Pandora Sync is on (S10).
            onClick={() => confirm(eligibility.mode === "sync" ? { mode: "play", syncReason: null } : eligibility)}
            data-testid="confirm-play"
            data-mode="play"
            aria-describedby={pandoraNote ? "pandora-note" : undefined}
          >
            {playButtonLabel(names)}
          </button>
          {pandoraSyncOffered(play!.content_ref, airplay, syncActive) ? (
            // Phase 7 (experimental): plain text, never amber. Amber means a live audio state the hub
            // controls; this hands the station off to the hub Mac, which streams to the chosen rooms
            // over AirPlay 2. Needs at least one room; the hub maps rooms to outputs.
            <>
              <button
                type="button"
                className="flex min-h-target w-full items-center justify-center rounded-control text-body text-secondary disabled:opacity-40"
                disabled={playSel.length === 0}
                onClick={() => {
                  // Pin the first bridged room as the Now Playing side (as confirm does) so the
                  // mini-player and Now Playing show the Pandora Sync chip for it.
                  rememberTargets(playSel, refKey(play!.content_ref));
                  setTargets(playSel);
                  selectSide(playSel[0]!);
                  onPandoraSync?.(playSel);
                  onClose();
                }}
                aria-describedby="pandora-sync-note"
                data-testid="pandora-sync"
              >
                {PANDORA_SYNC_BUTTON}
              </button>
              <p id="pandora-sync-note" className="text-center text-micro text-secondary" data-testid="pandora-sync-note">
                {pandoraSyncNote(names)}
              </p>
            </>
          ) : null}
          {eligibility.mode === "play" && eligibility.syncReason && play!.content_ref.kind !== "station" ? (
            // Both vendors selected but the content cannot sync: say why instead of silently falling back.
            <div className="flex flex-col items-center gap-1">
              <button type="button" className="flex h-target w-full items-center justify-center rounded-control border border-stroke text-body text-secondary" disabled aria-disabled="true" data-testid="sync-play-disabled">
                {SYNC_BUTTON}
              </button>
              <p className="text-micro text-tertiary" data-testid="sync-reason">
                {eligibility.syncReason}
              </p>
            </div>
          ) : null}
        </div>
      ) : null}
    </Sheet>
  );
}

export type { HubState };
