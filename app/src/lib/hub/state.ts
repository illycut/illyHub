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
    sync: {
      status: "idle",
      master_side: null,
      follower_side: null,
      content_ref: null,
      drift_ms: null,
      start_delta_ms: null,
      last_correction_at: null,
      corrections: 0,
      reason: null,
      started_at: null,
      session_id: null,
      title: null,
    },
    connections: {},
    pandora_sync: emptyPandoraSync(),
  };
}

export function emptyPandoraSync(): NonNullable<HubState["pandora_sync"]> {
  return { active: false, side_ids: [], output_ids: [], outputs: [], previous_output_ids: [], started_at: null, note: null };
}

const strings = (x: unknown): string[] => (Array.isArray(x) ? x.filter((v): v is string => typeof v === "string") : []);

/** Coerce a `pandora_sync` payload (snapshot or delta); null/garbage reads as inactive. */
export function asPandoraSync(x: unknown): NonNullable<HubState["pandora_sync"]> {
  if (!x || typeof x !== "object") return emptyPandoraSync();
  const r = x as Record<string, unknown>;
  return {
    active: r.active === true,
    side_ids: strings(r.side_ids),
    output_ids: strings(r.output_ids),
    outputs: strings(r.outputs),
    previous_output_ids: strings(r.previous_output_ids),
    started_at: typeof r.started_at === "string" ? r.started_at : null,
    note: typeof r.note === "string" ? r.note : null,
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
    if (path === "pandora_sync") {
      // Depth-one like `sync`; null means the bridge went away (inactive).
      next.pandora_sync = asPandoraSync(value);
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
