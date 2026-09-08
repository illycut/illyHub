/**
 * Pandora view logic (PRD §3.4a HOME-5, §3.5 PAN-3 and notes; design system §8, §10). Pure
 * functions so the home section, the target picker and the store agree.
 *
 * Facts baked in: Pandora is linked inside each vendor's own app, not through the hub, so there
 * is no "Connect Pandora" flow and the hub reports per-vendor link state (`linked`) alongside
 * per-station availability; accounts usually allow one stream, so a second room may pause the
 * first; stations cannot seek and never take part in Sync Play (stations are not Tidal content).
 */
import type { Availability, LibraryItem, Section, VendorLinked } from "./hub/library";
import type { HubState, Side } from "./hub/types";
import { joinRooms } from "./sync";
import { PANDORA_CONCURRENT_MSG, VENDOR_APP, VENDOR_LABEL, isService, reasonFor, roomsOnlyLabel, serviceUnavailableCopy, type AvailabilityReason, type Vendor } from "./services";

export { VENDOR_APP, VENDOR_LABEL, type Vendor };

/** One sentence, fact and fix (§10): where Pandora gets linked for a vendor that lacks it. */
export function pandoraSetupNote(missing: Vendor[]): string | null {
  if (missing.length === 0) return null;
  if (missing.length === 1) return serviceUnavailableCopy("pandora", missing[0]!, "not_linked");
  return `Pandora is set up in the ${VENDOR_APP.heos} and the ${VENDOR_APP.sonos}.`;
}

/** Vendors the hub reports as not linked (`linked[vendor] === false`); null/true never count. */
export function unlinkedVendors(linked: Record<string, boolean | null> | VendorLinked | null | undefined): Vendor[] {
  return (["heos", "sonos"] as Vendor[]).filter((v) => (linked as Record<string, boolean | null> | null | undefined)?.[v] === false);
}

/**
 * Row reason for a side whose vendor cannot play this content (service-aware, §10): one sentence
 * form for every service, from the hub's templates. The reason is the hub's (`reasons[vendor]`)
 * when it sends one; otherwise an unlinked vendor gets the fix ("Pandora is set up in the HEOS
 * app."), a linked vendor whose account lacks the station gets the fact ("Not in the HEOS Pandora
 * account."), and any other service reads as not linked.
 */
export function unavailableCopy(service: string | undefined, vendor: Vendor, opts: { reason?: AvailabilityReason | string | null; unlinked?: readonly Vendor[]; availability?: Availability | null } = {}): string {
  const svc = isService(service) ? service : "tidal";
  const reason = opts.reason ?? reasonFor(opts.availability, vendor, svc, opts.unlinked ?? []);
  return serviceUnavailableCopy(svc, vendor, reason);
}

/**
 * Home section note (design §13 1.3): names the rooms that cannot play the stations and where to
 * fix it. Room names come from the hub's sides; with none known, "HEOS rooms".
 */
export function stationsNote(missing: readonly Vendor[], roomsByVendor: Partial<Record<Vendor, string[]>>): string | null {
  if (missing.length === 0) return null;
  return missing
    .map((v) => {
      const rooms = roomsByVendor[v] ?? [];
      const who = rooms.length ? joinRooms(rooms) : `${VENDOR_LABEL[v]} rooms`;
      return `${who} can't play these until Pandora is added in the ${VENDOR_APP[v]}.`;
    })
    .join(" ");
}

/** Card subtitle from availability: both vendors → none; one-sided → "HEOS rooms only"; neither → null (a card never says "Not available"). */
export function stationSubtitle(availability: Availability | null | undefined): string | null {
  const label = roomsOnlyLabel(availability);
  return label === "Not available" ? null : label;
}

/** The hub's PANDORA_CONCURRENT_MSG, from messages.json. */
export const PANDORA_CONCURRENT_NOTE = PANDORA_CONCURRENT_MSG;

/** Toast for a hub warning: name the room the hub says may pause, else the hub's own sentence. */
export function warningToast(w: { code: string; message: string; target?: string | null }, sides: Record<string, { name: string }> | undefined): string {
  if (w.code === "pandora_concurrent" && w.target) {
    const room = sides?.[w.target]?.name;
    if (room) return `${room} may pause: Pandora usually allows one stream per account.`;
  }
  return w.message;
}

export interface StationsSectionModel {
  /** Shown when there is something to say: stations, or a hub-side error for this section. */
  visible: boolean;
  /** Alphabetical by title, case- and accent-insensitive. */
  items: LibraryItem[];
  /** Vendors the hub reports as not linked (`linked[vendor] === false`); null/true never count. */
  missing: Vendor[];
  /** The note under the heading, only when there are stations to show, else null. */
  note: string | null;
}

/**
 * Home "Pandora stations" section rule. Link state comes from the hub's `linked` field: false means
 * that vendor's app has no Pandora (say where to fix it), null means the hub could not tell (say
 * nothing). Nothing to show hides the section; a section error shows through the grid's alert.
 */
export function stationsSectionModel(section: Section<LibraryItem> | null | undefined, roomsByVendor: Partial<Record<Vendor, string[]>> = {}): StationsSectionModel {
  const items = [...(section?.items ?? [])].sort((a, b) => a.title.localeCompare(b.title, undefined, { sensitivity: "base" }));
  const missing = unlinkedVendors(section?.linked);
  const visible = items.length > 0 || !!section?.error;
  return { visible, items, missing, note: items.length > 0 ? stationsNote(missing, roomsByVendor) : null };
}

/** Side ids currently playing Pandora, by the hub's now-playing source. */
export function pandoraPlayingSides(state: HubState | null | undefined): string[] {
  if (!state) return [];
  return Object.values(state.sides)
    .filter((s: Side) => s.play_state === "play" && state.now_playing[s.id]?.source === "pandora")
    .map((s) => s.id);
}

/**
 * Picker note (PRD §3.5 account constraint): shown when starting a Pandora station would leave two
 * rooms streaming on one account — two or more rooms selected, or another room (any vendor)
 * already playing Pandora that is not part of the selection.
 */
export function pandoraConcurrentNote(service: string | undefined, selected: string[], state: HubState | null | undefined): string | null {
  if (service !== "pandora" || selected.length === 0) return null;
  if (selected.length >= 2) return PANDORA_CONCURRENT_NOTE;
  const elsewhere = pandoraPlayingSides(state).some((id) => !selected.includes(id));
  return elsewhere ? PANDORA_CONCURRENT_NOTE : null;
}
