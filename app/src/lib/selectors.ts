import type { HubState, Player, Side, Zone } from "./hub/types";

/**
 * Pure selectors over HubState. The list selectors are memoized on the input collection object
 * so components re-render only when the collection they read actually changed (1 Hz position
 * deltas replace `positions`, not `sides`).
 */
function memo1<A extends object, R>(fn: (a: A) => R): (a: A | null | undefined) => R {
  let lastArg: A | null = null;
  let lastResult: R | null = null;
  const empty = fn({} as A);
  return (a) => {
    if (!a) return empty;
    if (a === lastArg && lastResult !== null) return lastResult;
    lastArg = a;
    lastResult = fn(a);
    return lastResult;
  };
}

const sortByName = <T extends { name: string }>(xs: T[]) => xs.sort((a, b) => a.name.localeCompare(b.name));

const sidesOf = memo1((sides: Record<string, Side>) => sortByName(Object.values(sides)));
const playersOf = memo1((players: Record<string, Player>) => sortByName(Object.values(players)));
const zonesOf = memo1((zones: Record<string, Zone>) => sortByName(Object.values(zones)));
const liveOf = memo1((sides: Record<string, Side>) =>
  sidesOf(sides)
    .filter((s) => s.play_state === "play")
    .map((s) => s.id),
);

export function sidesList(state: HubState | null): Side[] {
  return sidesOf(state?.sides);
}
export function playersList(state: HubState | null): Player[] {
  return playersOf(state?.players);
}
export function zonesList(state: HubState | null): Zone[] {
  return zonesOf(state?.zones);
}
/** Sides with live audio: play_state === "play". Drives the zone dot cluster. */
export function liveSideIds(state: HubState | null): string[] {
  return liveOf(state?.sides);
}

/**
 * The side Now Playing shows: the explicit choice if it exists; else a playing side that has
 * now-playing metadata; else any side with now-playing; else the first playing side; else the
 * first side.
 */
export function resolveActiveSide(state: HubState | null, chosen: string | null): Side | null {
  if (!state) return null;
  if (chosen && state.sides[chosen]) return state.sides[chosen] ?? null;
  const sides = sidesList(state);
  const hasNp = (s: Side) => !!state.now_playing[s.id]?.title;
  return (
    sides.find((s) => s.play_state === "play" && hasNp(s)) ??
    sides.find(hasNp) ??
    sides.find((s) => s.play_state === "play") ??
    sides[0] ??
    null
  );
}

export function sideForPlayer(state: HubState | null, playerId: string): Side | null {
  if (!state) return null;
  return Object.values(state.sides).find((s) => s.member_ids.includes(playerId)) ?? null;
}

/** Zone dot cluster rule (design system §6.3, §8): one dot per output, count chip past four live. */
export const ZONE_DOT_MAX = 4;
export interface ZoneDot {
  live: boolean;
  online: boolean;
}
export type ZoneDotModel = { kind: "dots"; dots: ZoneDot[]; live: number; offline: number } | { kind: "count"; count: number; offline: number };

export function zoneDotModel(sides: Side[], liveIds: string[], players?: Record<string, Player>): ZoneDotModel {
  const liveSet = new Set(liveIds);
  const dots = sides.map((s) => {
    const coordinator = players?.[s.coordinator_player_id];
    return { live: liveSet.has(s.id), online: coordinator ? coordinator.online : true };
  });
  const live = dots.filter((d) => d.live).length;
  const offline = dots.filter((d) => !d.online).length;
  if (live > ZONE_DOT_MAX) return { kind: "count", count: live, offline };
  return { kind: "dots", dots, live, offline };
}

export function zoneDotsLabel(model: ZoneDotModel): string {
  const live = model.kind === "count" ? model.count : model.live;
  const parts = [`${live} room${live === 1 ? "" : "s"} playing`];
  if (model.offline > 0) parts.push(`${model.offline} offline`);
  return parts.join(", ");
}

/** Denon zone(s) that power a side's players, if any. */
export function zonesForSide(state: HubState | null, side: Side | null): Zone[] {
  if (!state || !side) return [];
  return Object.values(state.zones).filter((z) => (z.player_ids ?? []).some((p) => side.member_ids.includes(p)));
}

export function hubDegraded(state: HubState | null): boolean {
  if (!state) return false;
  return Object.values(state.connections).some((c) => c.state === "reconnecting" || c.state === "disconnected");
}

/** Hub linked-volume rule mirrored for optimistic UI: loudest online player is the master, ratios preserved. */
export function linkedVolumeLevels(players: Record<string, Player>, arg: { level?: number; delta?: number }): Record<string, number> {
  const online = Object.values(players).filter((p) => p.online);
  if (online.length === 0) return {};
  const master = Math.max(...online.map((p) => p.volume));
  const target = Math.max(0, Math.min(100, arg.level !== undefined ? arg.level : master + (arg.delta ?? 0)));
  const out: Record<string, number> = {};
  for (const p of online) {
    if (master === 0) out[p.id] = target;
    else {
      const v = Math.floor((p.volume / master) * target + 0.5);
      out[p.id] = target > 0 && p.volume > 0 ? Math.max(1, Math.min(100, v)) : Math.min(100, v);
    }
  }
  return out;
}

// ---------------------------------------------------------------------------------------------
// State-keyed memoized view models. A zustand selector must return a stable reference when its
// inputs did not change, or the component re-renders forever; these cache on the collection
// identities they read (sides, players, zones), so 1 Hz position deltas leave them untouched.

const EMPTY_DOTS: ZoneDotModel = { kind: "dots", dots: [], live: 0, offline: 0 };
let dotsCache: { sides: object; players: object | undefined; model: ZoneDotModel } | null = null;

export function zoneDotsForState(state: HubState | null): ZoneDotModel {
  if (!state) return EMPTY_DOTS;
  if (dotsCache && dotsCache.sides === state.sides && dotsCache.players === state.players) return dotsCache.model;
  const model = zoneDotModel(sidesList(state), liveSideIds(state), state.players);
  dotsCache = { sides: state.sides, players: state.players, model };
  return model;
}

export interface ZoneRow {
  side: Side;
  zones: Zone[];
  coordinatorOnline: boolean;
}
const EMPTY_ROWS: ZoneRow[] = [];
let rowsCache: { sides: object; players: object; zones: object; rows: ZoneRow[] } | null = null;

export function zoneRowsForState(state: HubState | null): ZoneRow[] {
  if (!state) return EMPTY_ROWS;
  if (rowsCache && rowsCache.sides === state.sides && rowsCache.players === state.players && rowsCache.zones === state.zones) return rowsCache.rows;
  const rows = sidesList(state).map((side) => ({
    side,
    zones: zonesForSide(state, side),
    coordinatorOnline: state.players[side.coordinator_player_id]?.online ?? true,
  }));
  rowsCache = { sides: state.sides, players: state.players, zones: state.zones, rows };
  return rows;
}
