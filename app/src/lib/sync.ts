/**
 * Sync Play view logic (PRD CTL-5/6, design system §6.6–§6.8, §10). Pure functions over
 * HubState so the picker, chip, Now Playing and mini-player agree.
 *
 * Constraints baked in (docs/prd-review.md §6a, docs/api.md "Sync Play"): a session is one HEOS
 * side (clock master) plus one Sonos side (follower); extra players per vendor are grouped by the
 * hub first. Only Tidal content syncs. Copy never says "perfect".
 */
import type { ContentRef, Detail, HistoryItem } from "./hub/library";
import type { Side, SyncState, SyncStatus } from "./hub/types";

export const SYNC_NOTE = "Close, not perfect.";
export const SYNC_BUTTON = "Sync Play";
/** One phrase for one fact (§10): the picker reason and the hub-refusal toast say the same thing. */
export const SYNC_UNSUPPORTED_TOAST = "Sync Play works with Tidal content.";
/** Picker caption for a station with both vendors selected (design §13 1.3): no button, just the fact. */
export const STATIONS_CANT_SYNC = "Stations can't sync: Pandora picks different songs for each room.";
export const STOP_SYNC = "Stop sync";
export const RETRY = "Retry";
/** Picker note when Sync Play was launched from the Now Playing offer (UX S6). */
export const SYNC_OFFER_NOTE = "Restarts from the current track on both.";

/** A session exists and the user can act on it (chip visible): includes "lost". */
const ACTIVE: ReadonlySet<SyncStatus> = new Set(["resolving", "priming", "verifying", "starting", "locked", "drifting", "correcting", "lost"]);
/**
 * Statuses in which the hub mirrors master-side transport to the follower and both sides carry
 * live audio for the session (docs/api.md "Transport during Sync Play"). Not lost/stopped/idle.
 */
const MIRRORED: ReadonlySet<SyncStatus> = new Set(["resolving", "priming", "verifying", "starting", "locked", "drifting", "correcting"]);
const STARTING: ReadonlySet<SyncStatus> = new Set(["resolving", "priming", "verifying", "starting"]);

export function isSyncActive(sync: SyncState | null | undefined): boolean {
  return !!sync && ACTIVE.has(sync.status);
}

export function isSyncMirrored(sync: SyncState | null | undefined): boolean {
  return !!sync && MIRRORED.has(sync.status);
}

function sidesOf(sync: SyncState): string[] {
  return [sync.master_side, sync.follower_side].filter((x): x is string => !!x);
}

/** Sides that belong to the session (master first) while it is active, including lost. */
export function syncSideIds(sync: SyncState | null | undefined): string[] {
  return sync && isSyncActive(sync) ? sidesOf(sync) : [];
}

/** Sides the session is actually driving right now: both count as live for the zone dots. */
export function mirroredSideIds(sync: SyncState | null | undefined): string[] {
  return sync && isSyncMirrored(sync) ? sidesOf(sync) : [];
}

/**
 * Where a transport command for `sideId` goes. While the hub is mirroring, either synced side
 * routes to the master; once the session is lost or over, each side is on its own again.
 */
export function transportTargetFor(sideId: string, sync: SyncState | null | undefined): string {
  if (!sync || !isSyncMirrored(sync) || !sidesOf(sync).includes(sideId)) return sideId;
  return sync.master_side ?? sideId;
}

/** Glyph shape carries the state alongside color (§9): one inline SVG per chip, no separate dot. */
export type ChipShape = "dotted" | "filled" | "half" | "ring";

export interface ChipModel {
  text: "Starting" | "Synced" | "Adjusting" | "Sync lost";
  /** Text-color token class for the glyph. */
  tone: "text-sync-drift" | "text-sync-locked" | "text-sync-lost";
  shape: ChipShape;
  lost: boolean;
}

/** §6.7 chip mapping; null when there is nothing to show. */
export function chipModel(sync: SyncState | null | undefined): ChipModel | null {
  if (!sync || !isSyncActive(sync)) return null;
  if (STARTING.has(sync.status)) return { text: "Starting", tone: "text-sync-drift", shape: "dotted", lost: false };
  if (sync.status === "locked") return { text: "Synced", tone: "text-sync-locked", shape: "filled", lost: false };
  if (sync.status === "lost") return { text: "Sync lost", tone: "text-sync-lost", shape: "ring", lost: true };
  return { text: "Adjusting", tone: "text-sync-drift", shape: "half", lost: false };
}

/** Room names joined with " and " (grouped sides are already named "Kitchen + Patio", so "+" is ambiguous). */
export function joinRooms(names: string[]): string {
  if (names.length <= 1) return names[0] ?? "";
  return `${names.slice(0, -1).join(", ")} and ${names[names.length - 1]}`;
}

/** Full label for aria and the picker: "Syncing Living Room Amp and Kitchen + Patio". */
export function syncLabel(sync: SyncState | null | undefined, sides: Record<string, Side>): string | null {
  const ids = syncSideIds(sync);
  if (ids.length === 0) return null;
  return `Syncing ${joinRooms(ids.map((id) => sides[id]?.name ?? "another room"))}`;
}

/** Short header label that never wraps: "Syncing 2 rooms". */
export function syncLabelShort(sync: SyncState | null | undefined): string | null {
  const n = syncSideIds(sync).length;
  return n === 0 ? null : `Syncing ${n} rooms`;
}

export type Eligibility =
  | { mode: "sync"; heos: string[]; sonos: string[] }
  | { mode: "play"; syncReason: string | null };

/**
 * Picker button rule: at least one HEOS side and one Sonos side selected AND Tidal content →
 * Sync Play with every selected side per vendor (the hub groups them natively). Both vendors but
 * not Tidal → normal Play, with a disabled Sync Play and the reason (never a silent fallback).
 * One vendor → normal Play, no Sync Play shown.
 */
export function syncEligibility(selected: string[], sides: Record<string, Side>, ref: ContentRef | null | undefined): Eligibility {
  const heos = selected.filter((id) => sides[id]?.vendor === "heos");
  const sonos = selected.filter((id) => sides[id]?.vendor === "sonos");
  if (heos.length === 0 || sonos.length === 0) return { mode: "play", syncReason: null };
  if (ref?.kind === "station") return { mode: "play", syncReason: STATIONS_CANT_SYNC };
  if (ref?.service !== "tidal") return { mode: "play", syncReason: SYNC_UNSUPPORTED_TOAST };
  return { mode: "sync", heos, sonos };
}

/** Button copy: "Sync Play" for two rooms, "Sync Play in N rooms" beyond that. */
export function syncButtonLabel(roomCount: number): string {
  return roomCount > 2 ? `${SYNC_BUTTON} in ${roomCount} rooms` : SYNC_BUTTON;
}

/** Picker note under Sync Play: "Living Room Amp and Kitchen + Patio, together. Close, not perfect." */
export function syncNote(heosNames: string[], sonosNames: string[]): string {
  return `${joinRooms([...heosNames, ...sonosNames])}, together. ${SYNC_NOTE}`;
}

export interface SyncOffer {
  heos: string;
  sonos: string;
  /** Name of the room that would be added, for copy; never a raw side id. */
  partnerName: string;
}

/**
 * The offer is suppressed right after a stop: while the session that just ended (`stopped`)
 * still names the active side, the row does not immediately re-offer what the user just left.
 */
export function offerSuppressed(sync: SyncState | null | undefined, activeSideId: string | null): boolean {
  return !!sync && sync.status === "stopped" && !!activeSideId && sidesOf(sync).includes(activeSideId);
}

/**
 * Now Playing's secondary Sync Play action: the active side plays a track with a canonical Tidal
 * `content_ref` and the other vendor has an online side. The partner is that vendor's currently
 * playing side, else its most recently used side, else its first online side.
 */
export function syncOffer(
  active: Side | null,
  contentRef: { service: string; kind: string; id: string } | null | undefined,
  sides: Record<string, Side>,
  onlineOf: (side: Side) => boolean,
  sync: SyncState | null | undefined,
  recentSideIds: string[] = [],
): SyncOffer | null {
  if (!active || !contentRef || contentRef.service !== "tidal" || isSyncActive(sync) || offerSuppressed(sync, active.id)) return null;
  const candidates = Object.values(sides).filter((s) => s.vendor !== active.vendor && onlineOf(s));
  if (candidates.length === 0) return null;
  const partner =
    candidates.find((s) => s.play_state === "play") ??
    recentSideIds.map((id) => candidates.find((s) => s.id === id)).find((s): s is Side => !!s) ??
    candidates.sort((a, b) => a.name.localeCompare(b.name))[0]!;
  const partnerName = partner.name || "another room";
  return active.vendor === "heos" ? { heos: active.id, sonos: partner.id, partnerName } : { heos: partner.id, sonos: active.id, partnerName };
}

/**
 * What the offer plays: the active side's most recent album/playlist from history when its
 * cached detail contains the current track (start there), else the bare track ref.
 */
export function offerContent(
  trackRef: { service: string; kind: string; id: string },
  activeSideId: string,
  recents: HistoryItem[] | null | undefined,
  details: Record<string, Detail | null | undefined>,
  keyOf: (ref: ContentRef) => string,
): { content_ref: ContentRef; start_index?: number; title?: string; subtitle?: string | null } {
  const containers = (recents ?? []).filter((h) => (h.content_ref.kind === "album" || h.content_ref.kind === "playlist") && h.last_targets.includes(activeSideId));
  for (const h of containers) {
    const detail = details[keyOf(h.content_ref)];
    const track = detail?.tracks.find((t) => t.content_ref.id === trackRef.id);
    if (track) return { content_ref: h.content_ref, start_index: track.index, title: h.title, subtitle: h.subtitle };
  }
  return { content_ref: { service: "tidal", kind: "track", id: trackRef.id } as ContentRef };
}
