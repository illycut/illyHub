import type { DeltaCollection, HubState, ServerMessage } from "./types";

export function emptyState(): HubState {
  return {
    version: 0,
    players: {},
    zones: {},
    groups: {},
    sides: {},
    now_playing: {},
    positions: {},
    sync: { status: "idle", side_ids: [], drift_ms: null, last_correction_at: null },
    connections: {},
  };
}

const COLLECTIONS: ReadonlySet<string> = new Set<DeltaCollection>([
  "players",
  "zones",
  "groups",
  "sides",
  "now_playing",
  "positions",
  "connections",
]);

export type DeltaMessage = Extract<ServerMessage, { type: "delta" }>;

/**
 * Apply a hub delta. Paths are depth two (`players.heos-1`) with the full new value, or `null`
 * when removed. `sync` is depth one. Returns `null` when `from_version` does not match the
 * version we hold, which means the caller must resync (docs/api.md).
 */
export function applyDelta(state: HubState, delta: DeltaMessage): HubState | null {
  if (delta.from_version !== state.version) return null;
  const next: HubState = { ...state, version: delta.to_version };
  const touched = new Set<DeltaCollection>();
  for (const [path, value] of Object.entries(delta.changed)) {
    if (path === "sync") {
      if (value !== null && typeof value === "object") next.sync = value as HubState["sync"];
      continue;
    }
    if (path === "version") continue;
    const dot = path.indexOf(".");
    if (dot < 0) continue;
    const collection = path.slice(0, dot);
    const key = path.slice(dot + 1);
    if (!COLLECTIONS.has(collection)) continue;
    const c = collection as DeltaCollection;
    if (!touched.has(c)) {
      next[c] = { ...(state[c] as Record<string, unknown>) } as never;
      touched.add(c);
    }
    const bucket = next[c] as Record<string, unknown>;
    if (value === null) delete bucket[key];
    else bucket[key] = value;
  }
  return next;
}
