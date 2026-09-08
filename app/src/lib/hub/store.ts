/**
 * Hub state store. Holds the latest HubState (hydrated from a snapshot, patched by deltas),
 * connection phase, hub clock offset, the UI's target selection, and optimistic command
 * bookkeeping.
 *
 * Optimistic flow: `dispatch` applies a local patch and sends the REST command. The REST ack (or
 * the same ack on the socket) settles it. On ANY failure (error ack, network error, 5 s without
 * an ack) the optimistic layer is dropped by requesting a resync: the next snapshot replaces
 * state wholesale, so we never restore stale collections over newer hub state (docs/api.md,
 * "Ack timeout"). A snapshot clears every pending command.
 */
import { create } from "zustand";
import { commands as C, sendCommand, type CommandRequest, type Fetcher } from "./commands";
import { newCorrelationId } from "./correlation";
import { applyDelta, emptyState, type DeltaMessage } from "./state";
import type { Ack, HubState, Position, ServerMessage, TransportAction, Vendor } from "./types";
import type { ConnectionPhase } from "./socket";
import { toast } from "../ui/toasts";
import { linkedVolumeLevels, sideForPlayer } from "../selectors";
import { interpolatePosition } from "../position";

export const ACK_TIMEOUT_MS = 5000;

type Patch = (s: HubState) => HubState;
export type TimerHandle = unknown;

interface Pending {
  timer: TimerHandle | null;
  label: string;
}

export interface HubStore {
  state: HubState | null;
  phase: ConnectionPhase;
  lastAttemptAt: number | null;
  everConnected: boolean;
  /** hubTime - localTime, ms. Positions are interpolated on the hub's clock. */
  clockOffsetMs: number;
  activeSideId: string | null;
  selectedTargets: string[];
  pending: Record<string, Pending>;
  /** Hook the socket installs so the store can request a resync. */
  requestResync: () => void;

  // protocol
  onMessage(msg: ServerMessage): void;
  setPhase(phase: ConnectionPhase, at: number): void;
  setClockOffset(ms: number): void;
  setResync(fn: () => void): void;
  /** Current time on the hub's clock. */
  hubNow(): number;

  // selection
  selectSide(id: string | null): void;
  toggleTarget(id: string): void;
  setTargets(ids: string[]): void;

  // commands
  dispatch(req: CommandRequest, optimistic?: Patch, label?: string): Promise<Ack>;
  transport(action: TransportAction, target: string): Promise<Ack>;
  seek(sideId: string, positionMs: number): Promise<Ack>;
  skip(sideId: string, deltaMs: number): Promise<Ack>;
  setVolume(playerId: string, level: number): Promise<Ack>;
  setSideVolume(sideId: string, level: number): Promise<Ack>;
  linkedVolume(arg: { level?: number; delta?: number }): Promise<Ack>;
  setMute(target: string, muted: boolean): Promise<Ack>;
  zonePower(zoneId: string, on: boolean): Promise<Ack>;
  setGroup(vendor: Vendor, coordinatorId: string, memberIds: string[]): Promise<Ack>;
  ungroup(sideId: string): Promise<Ack>;

  /** Test seams. */
  _deps: {
    fetcher: Fetcher;
    now: () => number;
    setTimer: (fn: () => void, ms: number) => TimerHandle;
    clearTimer: (t: TimerHandle) => void;
  };
  _reset(): void;
}

const defaultDeps: HubStore["_deps"] = {
  fetcher: (...args) => fetch(...args),
  now: () => Date.now(),
  setTimer: (fn, ms) => setTimeout(fn, ms),
  clearTimer: (t) => clearTimeout(t as ReturnType<typeof setTimeout>),
};

export const useHub = create<HubStore>((set, get) => ({
  state: null,
  phase: "connecting",
  lastAttemptAt: null,
  everConnected: false,
  clockOffsetMs: 0,
  activeSideId: null,
  selectedTargets: [],
  pending: {},
  requestResync: () => {},
  _deps: defaultDeps,

  onMessage(msg) {
    if (msg.type === "snapshot") {
      // A snapshot is authoritative: every optimistic command is superseded.
      for (const p of Object.values(get().pending)) if (p.timer) get()._deps.clearTimer(p.timer);
      set({ state: msg.state, pending: {} });
      return;
    }
    if (msg.type === "delta") {
      const cur = get().state;
      if (!cur) {
        get().requestResync();
        return;
      }
      const next = applyDelta(cur, msg as DeltaMessage);
      if (!next) {
        get().requestResync();
        return;
      }
      set({ state: next });
      return;
    }
    if (msg.type === "ack") {
      settle(get, set, msg);
      return;
    }
    // pong / error: nothing to store
  },

  setPhase(phase, at) {
    set((s) => ({ phase, lastAttemptAt: at, everConnected: s.everConnected || phase === "open" }));
  },

  setClockOffset(ms) {
    set({ clockOffsetMs: ms });
  },

  setResync(fn) {
    set({ requestResync: fn });
  },

  hubNow() {
    return get()._deps.now() + get().clockOffsetMs;
  },

  selectSide(id) {
    set({ activeSideId: id });
  },
  toggleTarget(id) {
    set((s) => ({
      selectedTargets: s.selectedTargets.includes(id)
        ? s.selectedTargets.filter((t) => t !== id)
        : [...s.selectedTargets, id],
    }));
  },
  setTargets(ids) {
    set({ selectedTargets: ids });
  },

  async dispatch(req, optimistic, label = req.path) {
    const deps = get()._deps;
    const cid = req.correlationId ?? newCorrelationId();
    const before = get().state;
    if (optimistic && before) set({ state: optimistic(before) });

    const timer = deps.setTimer(() => {
      const p = get().pending[cid];
      if (!p) return;
      fail(get, set, cid, `${p.label} didn't get confirmed by the hub.`);
    }, ACK_TIMEOUT_MS);
    set((s) => ({ pending: { ...s.pending, [cid]: { timer, label } } }));

    try {
      const ack = await sendCommand({ ...req, correlationId: cid }, deps.fetcher);
      settle(get, set, ack);
      return ack;
    } catch {
      const message = "Can't reach the hub. Check that the Mac is on the network.";
      if (get().pending[cid]) fail(get, set, cid, message);
      return {
        correlation_id: cid,
        ok: false,
        action: label,
        target: null,
        state_version: 0,
        error: { code: "adapter_disconnected", message, target: null, correlation_id: cid },
        partial: [],
      } as Ack;
    }
  },

  transport(action, target) {
    const patch: Patch | undefined =
      action === "play" || action === "pause" || action === "toggle" || action === "stop"
        ? (s) => {
            const side = s.sides[target] ?? sideForPlayer(s, target);
            if (!side) return s;
            const cur = side.play_state;
            const nextState =
              action === "play" ? "play" : action === "pause" ? "pause" : action === "stop" ? "stop" : cur === "play" ? "pause" : "play";
            return { ...s, sides: { ...s.sides, [side.id]: { ...side, play_state: nextState } } };
          }
        : undefined;
    return get().dispatch(C.transport(action, target), patch, actionLabel(action));
  },

  seek(sideId, positionMs) {
    const patch: Patch = (s) => {
      const pos: Position = { position_ms: Math.max(0, Math.round(positionMs)), reported_at: new Date(get().hubNow()).toISOString(), confidence: 0.5 };
      return { ...s, positions: { ...s.positions, [sideId]: pos } };
    };
    return get().dispatch(C.seek(sideId, positionMs), patch, "Seek");
  },

  skip(sideId, deltaMs) {
    const patch: Patch = (s) => {
      const side = s.sides[sideId];
      const dur = s.now_playing[sideId]?.duration_ms ?? null;
      const base = interpolatePosition(s.positions[sideId], side?.play_state, get().hubNow(), dur);
      const pos: Position = {
        position_ms: Math.max(0, Math.min(dur ?? Number.POSITIVE_INFINITY, base + deltaMs)),
        reported_at: new Date(get().hubNow()).toISOString(),
        confidence: 0.5,
      };
      return { ...s, positions: { ...s.positions, [sideId]: pos } };
    };
    return get().dispatch(C.skip(sideId, deltaMs), patch, deltaMs < 0 ? "Skip back" : "Skip forward");
  },

  setVolume(playerId, level) {
    const patch: Patch = (s) => {
      const p = s.players[playerId];
      if (!p) return s;
      return { ...s, players: { ...s.players, [playerId]: { ...p, volume: Math.round(level) } } };
    };
    const name = get().state?.players[playerId]?.name ?? "that room";
    return get().dispatch(C.volume(playerId, level), patch, `Volume on ${name}`);
  },

  setSideVolume(sideId, level) {
    const patch: Patch = (s) => {
      const side = s.sides[sideId];
      if (!side) return s;
      const players = { ...s.players };
      for (const pid of side.member_ids) {
        const p = players[pid];
        if (p) players[pid] = { ...p, volume: Math.round(level) };
      }
      return { ...s, players, sides: { ...s.sides, [sideId]: { ...side, volume: Math.round(level) } } };
    };
    return get().dispatch(C.volume(sideId, level), patch, "Volume");
  },

  linkedVolume(arg) {
    const patch: Patch = (s) => {
      const levels = linkedVolumeLevels(s.players, arg);
      const players = { ...s.players };
      for (const [pid, v] of Object.entries(levels)) {
        const p = players[pid];
        if (p) players[pid] = { ...p, volume: v };
      }
      const sides = { ...s.sides };
      for (const side of Object.values(sides)) {
        const vols = side.member_ids.map((m) => players[m]?.volume).filter((v): v is number => v !== undefined);
        if (vols.length) sides[side.id] = { ...side, volume: Math.round(vols.reduce((a, b) => a + b, 0) / vols.length) };
      }
      return { ...s, players, sides };
    };
    return get().dispatch(C.linkedVolume(arg), patch, "Master volume");
  },

  setMute(target, muted) {
    const patch: Patch = (s) => {
      const p = s.players[target];
      if (p) return { ...s, players: { ...s.players, [target]: { ...p, muted } } };
      const side = s.sides[target];
      if (!side) return s;
      const players = { ...s.players };
      for (const pid of side.member_ids) {
        const m = players[pid];
        if (m) players[pid] = { ...m, muted };
      }
      return { ...s, players, sides: { ...s.sides, [target]: { ...side, muted } } };
    };
    return get().dispatch(C.mute(target, muted), patch, muted ? "Mute" : "Unmute");
  },

  zonePower(zoneId, on) {
    const patch: Patch = (s) => {
      const z = s.zones[zoneId];
      if (!z) return s;
      return { ...s, zones: { ...s.zones, [zoneId]: { ...z, power: on } } };
    };
    const name = get().state?.zones[zoneId]?.name ?? "the zone";
    return get().dispatch(C.zonePower(zoneId, on), patch, `${on ? "Turn on" : "Turn off"} ${name}`);
  },

  setGroup(vendor, coordinatorId, memberIds) {
    return get().dispatch(C.group(vendor, coordinatorId, memberIds), undefined, "Group change");
  },

  ungroup(sideId) {
    return get().dispatch(C.ungroup(sideId), undefined, "Ungroup");
  },

  _reset() {
    for (const p of Object.values(get().pending)) if (p.timer) get()._deps.clearTimer(p.timer);
    set({
      state: null,
      phase: "connecting",
      lastAttemptAt: null,
      everConnected: false,
      clockOffsetMs: 0,
      activeSideId: null,
      selectedTargets: [],
      pending: {},
      requestResync: () => {},
      _deps: defaultDeps,
    });
  },
}));

type Get = () => HubStore;
type Set = (partial: Partial<HubStore> | ((s: HubStore) => Partial<HubStore>)) => void;

function finishPending(get: Get, set: Set, cid: string): void {
  const p = get().pending[cid];
  if (p?.timer) get()._deps.clearTimer(p.timer);
  set((s) => {
    const rest = { ...s.pending };
    delete rest[cid];
    return { pending: rest };
  });
}

/** Any failure: drop the optimistic layer via resync and tell the user what did not happen. */
function fail(get: Get, set: Set, cid: string, message: string): void {
  finishPending(get, set, cid);
  toast(message);
  get().requestResync();
}

/** Resolve an ack against pending commands. Acks for other clients' commands (or duplicates) are ignored. */
function settle(get: Get, set: Set, ack: Ack): void {
  const p = get().pending[ack.correlation_id];
  if (!p) return;
  if (!ack.ok) {
    fail(get, set, ack.correlation_id, ack.error?.message ?? `${p.label} didn't happen.`);
    return;
  }
  finishPending(get, set, ack.correlation_id);
  for (const f of ack.partial ?? []) toast(f.message);
}

function actionLabel(action: TransportAction): string {
  switch (action) {
    case "play":
      return "Play";
    case "pause":
      return "Pause";
    case "toggle":
      return "Play/pause";
    case "stop":
      return "Stop";
    case "next":
      return "Next track";
    case "prev":
      return "Previous track";
  }
}

export function emptyHubState(): HubState {
  return emptyState();
}
