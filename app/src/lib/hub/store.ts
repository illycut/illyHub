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
import type { ContentRef } from "./library";
import type { ConnectionPhase } from "./socket";
import { applyDelta, asPandoraSync, emptyPandoraSync, emptyState, type DeltaMessage } from "./state";
import type { Ack, ArtRef, HubState, PlayState, Position, ServerMessage, TransportAction, Vendor } from "./types";
import { interpolatePosition } from "../position";
import { linkedVolumeLevels, sideForPlayer } from "../selectors";
import { isPlayingState, isSettledState } from "../playState";
import { toast, type ToastAction } from "../ui/toasts";
import { warningToast } from "../pandora";
import { PANDORA_SYNC_FAILED, PANDORA_SYNC_STARTED, PANDORA_SYNC_STOPPED } from "../airplay";
import { SERVICE_LABEL, isHubLinked } from "../services";
import { SYNC_UNSUPPORTED_TOAST, transportTargetFor } from "../sync";

/** What `play()` needs from a library item to update the UI optimistically. */
export interface PlayItem {
  content_ref: ContentRef;
  title: string;
  subtitle: string | null;
  art: ArtRef;
  start_index?: number;
}

export const ACK_TIMEOUT_MS = 5000;

/** Error codes that mean "nothing on any side was touched" and end a multi-side play early. */
export const PLAY_SHORT_CIRCUIT_CODES = new Set(["needs_link", "not_found", "invalid_argument", "unknown_target"]);

/** "Connect …" toast action, only for services the hub links itself (Pandora is linked in the vendor apps). */
function connectAction(service: string): ToastAction | undefined {
  return isHubLinked(service) ? { label: `Connect ${SERVICE_LABEL[service]}`, href: `/settings?link=${service}` } : undefined;
}

type Patch = (s: HubState) => HubState;
export type TimerHandle = unknown;

/** What to do when a command fails: the default toasts and resyncs; "keep" leaves both to the caller. */
export type OnFail = "resync" | "keep";

export interface DispatchOptions {
  onFail?: OnFail;
}

interface Pending {
  timer: TimerHandle | null;
  label: string;
  onFail: OnFail;
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
  /**
   * Per-side transport intent from the last optimistic command, held until the hub reports a settled
   * state (play/pause/stop). While the hub says `unknown` (a device transient) the icon keeps showing
   * what the user asked for instead of flipping on the transitional delta.
   */
  intents: Record<string, PlayState>;
  /** Hook the socket installs so the store can request a resync. */
  requestResync: () => void;
  /** What the play/pause icon shows for a side: the held intent while the hub is transitional, else the hub's state. */
  displayPlayState(sideId: string | null | undefined): PlayState | undefined;

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
  dispatch(req: CommandRequest, optimistic?: Patch, label?: string, opts?: DispatchOptions): Promise<Ack>;
  transport(action: TransportAction, target: string): Promise<Ack>;
  seek(sideId: string, positionMs: number): Promise<Ack>;
  skip(sideId: string, deltaMs: number): Promise<Ack>;
  setVolume(playerId: string, level: number): Promise<Ack>;
  setSideVolume(sideId: string, level: number): Promise<Ack>;
  /**
   * An amplifier zone as a volume target in its own right (PRD §3.3 [1.2]): a zone driving an
   * external amp owns no player, so its level is set by zone id. The hub mirrors a zone's level onto
   * the players it owns; the optimistic patch does the same.
   */
  setZoneVolume(zoneId: string, level: number): Promise<Ack>;
  linkedVolume(arg: { level?: number; delta?: number }): Promise<Ack>;
  setMute(target: string, muted: boolean): Promise<Ack>;
  zonePower(zoneId: string, on: boolean): Promise<Ack>;
  setGroup(vendor: Vendor, coordinatorId: string, memberIds: string[]): Promise<Ack>;
  ungroup(sideId: string): Promise<Ack>;
  /**
   * Play library content on one or more sides (one command per side). Optimistically marks each
   * side playing and shows the item in now-playing so the mini-player updates before the hub
   * confirms. Resolves with every ack; failures toast per side.
   */
  play(sideIds: string[], item: PlayItem): Promise<Ack[]>;
  /**
   * Sync Play (Phase 4): one HEOS side (clock master) + one Sonos side (follower). Optimistically
   * marks both playing with the item; the hub's `sync` state drives the chip. Refusals toast in
   * design voice (needs_link → Connect action; unsupported_content → Tidal note).
   */
  syncPlay(heosTargets: string[], sonosTargets: string[], item: PlayItem): Promise<Ack>;
  syncStop(): Promise<Ack>;
  syncRetry(): Promise<Ack>;
  /**
   * Pandora Sync (Phase 7, experimental): the hub Mac plays Pandora and streams to AirPlay outputs
   * for the chosen sides. No optimistic state: the hub's `pandora_sync` delta drives the chip (as
   * `syncPlay` does). The hub's start/stop notes are toasted once; refusals are toasted verbatim.
   */
  pandoraSyncStart(sideIds: string[]): Promise<Ack>;
  pandoraSyncStop(): Promise<Ack>;

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
  intents: {},
  requestResync: () => {},
  _deps: defaultDeps,

  onMessage(msg) {
    if (msg.type === "snapshot") {
      // A snapshot is authoritative: every optimistic command is superseded.
      for (const p of Object.values(get().pending)) if (p.timer) get()._deps.clearTimer(p.timer);
      const state: HubState = { ...msg.state, pandora_sync: asPandoraSync(msg.state.pandora_sync) };
      set({ state, pending: {}, intents: settleIntents(get().intents, state) });
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
      set({ state: next, intents: settleIntents(get().intents, next) });
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
  displayPlayState(sideId) {
    if (!sideId) return undefined;
    const hub = get().state?.sides[sideId]?.play_state;
    if (hub === undefined) return undefined;
    const intent = get().intents[sideId];
    // The hub's settled word wins (settleIntents has already dropped the intent); while it is still
    // transitional (`unknown`) the user's last command is what the icon shows.
    return intent !== undefined && !isSettledState(hub) ? intent : hub;
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

  async dispatch(req, optimistic, label = req.path, opts = {}) {
    const deps = get()._deps;
    const cid = req.correlationId ?? newCorrelationId();
    const before = get().state;
    if (optimistic && before) set({ state: optimistic(before) });

    const timer = deps.setTimer(() => {
      const p = get().pending[cid];
      if (!p) return;
      fail(get, set, cid, `${p.label} didn't get confirmed by the hub.`);
    }, ACK_TIMEOUT_MS);
    set((s) => ({ pending: { ...s.pending, [cid]: { timer, label, onFail: opts.onFail ?? "resync" } } }));

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
    // While a Sync Play session is live, transport for either synced side goes to the master
    // and the hub mirrors it to the follower (docs/api.md "Sync Play").
    target = transportTargetFor(target, get().state?.sync);
    // Pin the side being controlled so Now Playing does not hop to another playing side when
    // this one pauses (the resolver prefers playing sides only while nothing is chosen).
    if (!get().activeSideId && get().state?.sides[target]) set({ activeSideId: target });
    const patch: Patch | undefined =
      action === "play" || action === "pause" || action === "toggle" || action === "stop"
        ? (s) => {
            const side = s.sides[target] ?? sideForPlayer(s, target);
            if (!side) return s;
            // A toggle acts on what the user sees: the held intent while the hub is transitional, else the hub's state.
            const shown = get().displayPlayState(side.id) ?? side.play_state;
            const nextState: PlayState =
              action === "play" ? "play" : action === "pause" ? "pause" : action === "stop" ? "stop" : isPlayingState(shown) ? "pause" : "play";
            // Hold the intent until the hub reports a settled state, so a transient `unknown` delta cannot flip the icon.
            set((st) => ({ intents: { ...st.intents, [side.id]: nextState } }));
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

  setZoneVolume(zoneId, level) {
    const v = Math.round(level);
    const patch: Patch = (s) => {
      const z = s.zones[zoneId];
      if (!z) return s;
      const players = { ...s.players };
      for (const pid of z.player_ids ?? []) {
        const p = players[pid];
        if (p) players[pid] = { ...p, volume: v };
      }
      return { ...s, players, zones: { ...s.zones, [zoneId]: { ...z, volume: v } } };
    };
    const name = get().state?.zones[zoneId]?.name ?? "that zone";
    return get().dispatch(C.volume(zoneId, level), patch, `Volume on ${name}`);
  },

  setMute(target, muted) {
    const patch: Patch = (s) => {
      const p = s.players[target];
      if (p) return { ...s, players: { ...s.players, [target]: { ...p, muted } } };
      const z = s.zones[target];
      if (z) {
        // A zone mutes itself and mirrors onto the players it owns.
        const players = { ...s.players };
        for (const pid of z.player_ids ?? []) {
          const m = players[pid];
          if (m) players[pid] = { ...m, muted };
        }
        return { ...s, players, zones: { ...s.zones, [target]: { ...z, muted } } };
      }
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

  async play(sideIds, item) {
    const patchFor =
      (sideId: string): Patch =>
      (s) => {
        const side = s.sides[sideId];
        if (!side) return s;
        const prev = s.now_playing[sideId];
        const np = {
          title: item.title,
          artist: item.subtitle,
          album: item.content_ref.kind === "album" ? item.title : (prev?.album ?? null),
          art: item.art,
          source: item.content_ref.service,
          seekable: side.capabilities?.supports_seek ?? prev?.seekable ?? true,
          supports_next: true,
          supports_prev: true,
          duration_ms: null,
          track_id: null,
          content_ref: null,
        };
        return {
          ...s,
          sides: { ...s.sides, [sideId]: { ...side, play_state: "play" } },
          now_playing: { ...s.now_playing, [sideId]: np },
          positions: { ...s.positions, [sideId]: { position_ms: 0, reported_at: new Date(get().hubNow()).toISOString(), confidence: 0.5 } },
        };
      };
    /** Undo the optimistic entries for one side. Only used when the hub touched nothing. */
    const revertFor = (sideId: string, before: HubState | null): Patch => (s) => {
      if (!before) return s;
      const sides = { ...s.sides };
      const now_playing = { ...s.now_playing };
      const positions = { ...s.positions };
      if (before.sides[sideId]) sides[sideId] = before.sides[sideId]!;
      if (before.now_playing[sideId]) now_playing[sideId] = before.now_playing[sideId]!;
      else delete now_playing[sideId];
      if (before.positions[sideId]) positions[sideId] = before.positions[sideId]!;
      else delete positions[sideId];
      return { ...s, sides, now_playing, positions };
    };
    const names = (id: string) => get().state?.sides[id]?.name ?? "that room";
    const acks: Ack[] = [];
    const service = item.content_ref.service;
    // Sequential so a "nothing was touched" failure on the first side short-circuits the rest
    // with a single toast and no resync (docs/api.md: needs_link is raised before any side).
    for (const id of sideIds) {
      const before = get().state;
      const ack = await get().dispatch(C.play(id, item.content_ref, item.start_index), patchFor(id), `Play on ${names(id)}`, { onFail: "keep" });
      acks.push(ack);
      if (ack.ok) {
        toastNotes(ack, get().state?.sides);
        continue;
      }
      const code = ack.error?.code ?? "vendor_error";
      if (PLAY_SHORT_CIRCUIT_CODES.has(code)) {
        set((s) => ({ state: s.state ? revertFor(id, before)(s.state) : s.state }));
        toast(
          ack.error?.message ?? `${item.title} didn't start.`,
          code === "needs_link" ? connectAction(service) : undefined,
        );
        break;
      }
      // Per-side failure (not_available_on_side, device_offline, vendor error): say which room,
      // drop this side's optimistic entries, keep going for the other sides.
      set((s) => ({ state: s.state ? revertFor(id, before)(s.state) : s.state }));
      toast(ack.error?.message ?? `${names(id)} didn't start ${item.title}.`);
    }
    return acks;
  },

  async syncPlay(heosTargets, sonosTargets, item) {
    const heosTarget = heosTargets[0] ?? "";
    const sonosTarget = sonosTargets[0] ?? "";
    const patch: Patch = (s) => {
      let next = s;
      for (const sideId of [...heosTargets, ...sonosTargets]) {
        const side = next.sides[sideId];
        if (!side) continue;
        const prev = next.now_playing[sideId];
        next = {
          ...next,
          sides: { ...next.sides, [sideId]: { ...side, play_state: "play" } },
          now_playing: {
            ...next.now_playing,
            [sideId]: {
              title: item.title,
              artist: item.subtitle,
              album: item.content_ref.kind === "album" ? item.title : (prev?.album ?? null),
              art: item.art,
              source: item.content_ref.service,
              seekable: false,
              supports_next: true,
              supports_prev: true,
              duration_ms: null,
              track_id: null,
              content_ref: null,
            },
          },
          positions: { ...next.positions, [sideId]: { position_ms: 0, reported_at: new Date(get().hubNow()).toISOString(), confidence: 0.5 } },
        };
      }
      return { ...next, sync: { ...next.sync, status: "resolving", master_side: heosTarget, follower_side: sonosTarget, content_ref: item.content_ref, title: item.title, reason: null } };
    };
    const one = (xs: string[]) => (xs.length === 1 ? xs[0]! : xs);
    const ack = await get().dispatch(C.syncPlay(item.content_ref, one(heosTargets), one(sonosTargets), item.start_index), patch, "Sync Play", { onFail: "keep" });
    if (ack.ok) {
      toastNotes(ack, get().state?.sides);
      return ack;
    }
    const code: string = ack.error?.code ?? "vendor_error";
    const service = item.content_ref.service;
    if (code === "needs_link") {
      toast(ack.error?.message ?? `${item.title} didn't start.`, connectAction(service));
    } else if (code === "unsupported_content") {
      toast(SYNC_UNSUPPORTED_TOAST);
    } else {
      toast(ack.error?.message ?? `Sync Play didn't start ${item.title}.`);
    }
    // The hub touched nothing (or reports what it did in `sync`): drop the optimistic layer.
    get().requestResync();
    return ack;
  },

  syncStop() {
    const patch: Patch = (s) => ({ ...s, sync: { ...s.sync, status: "stopped" } });
    return get().dispatch(C.syncStop(), patch, "Stop sync");
  },

  syncRetry() {
    const patch: Patch = (s) => ({ ...s, sync: { ...s.sync, status: "priming", reason: null } });
    return get().dispatch(C.syncRetry(), patch, "Sync retry");
  },

  async pandoraSyncStart(sideIds) {
    // No optimistic patch: the hub's `pandora_sync` delta drives the chip (S6).
    const ack = await get().dispatch(C.pandoraSyncStart(sideIds), undefined, "Pandora Sync", { onFail: "keep" });
    if (ack.ok) {
      toastNotes(ack, get().state?.sides);
      // Always say the hub's started note once (toast dedupe handles repeats): from the state the
      // delta landed on, else the hub's exported template (S7).
      const ps = get().state?.pandora_sync;
      toast((ps?.active ? ps.note : null) ?? PANDORA_SYNC_STARTED);
      return ack;
    }
    // Refusals verbatim: bridge_unavailable (503), invalid_argument (no matching outputs, names the
    // rooms), sync_active while Sync Play is live.
    toast(ack.error?.message ?? PANDORA_SYNC_FAILED);
    get().requestResync();
    return ack;
  },

  async pandoraSyncStop() {
    const patch: Patch = (s) => ({ ...s, pandora_sync: { ...(s.pandora_sync ?? emptyPandoraSync()), active: false } });
    const ack = await get().dispatch(C.pandoraSyncStop(), patch, "Stop Pandora Sync", { onFail: "keep" });
    if (ack.ok) {
      toastNotes(ack, get().state?.sides);
      toast(PANDORA_SYNC_STOPPED);
      return ack;
    }
    // Stop while idle is not an error for the user: nothing was running. Anything else is said verbatim.
    if (ack.error?.code !== "sync_idle") toast(ack.error?.message ?? "Pandora Sync didn't stop.");
    get().requestResync();
    return ack;
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
      intents: {},
      requestResync: () => {},
      _deps: defaultDeps,
    });
  },
}));

/** Drop every held intent whose side the hub now reports in a settled state (play/pause/stop). */
function settleIntents(intents: Record<string, PlayState>, state: HubState): Record<string, PlayState> {
  let changed = false;
  const out: Record<string, PlayState> = {};
  for (const [id, intent] of Object.entries(intents)) {
    const hub = state.sides[id]?.play_state;
    if (hub === undefined || isSettledState(hub)) changed = true;
    else out[id] = intent;
  }
  return changed ? out : intents;
}

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

/**
 * Any failure: by default drop the optimistic layer via resync and tell the user what did not
 * happen. With `onFail: "keep"` the caller owns both (used by `play`, which knows when the hub
 * touched nothing and can undo locally).
 */
function fail(get: Get, set: Set, cid: string, message: string): void {
  const p = get().pending[cid];
  finishPending(get, set, cid);
  if (p?.onFail === "keep") return;
  toast(message);
  get().requestResync();
}

/**
 * Non-fatal notes on a successful ack: per-target failures in `partial` and hub warnings such as
 * `pandora_concurrent`, both shown verbatim (the hub owns the copy, docs/api.md).
 */
export function toastNotes(ack: Ack, sides?: Record<string, { name: string }>): void {
  for (const f of ack.partial ?? []) toast(f.message);
  for (const w of ack.warnings ?? []) toast(warningToast(w, sides));
}

/**
 * Resolve an ack against pending commands. Acks for other clients' commands (or duplicates) are
 * ignored. Every command's ack arrives twice, once as the REST response and once on the WebSocket:
 * whichever lands first removes the pending entry, so the second call returns at `!p` and nothing
 * is toasted twice. With `onFail: "keep"` the caller (play, syncPlay) toasts from the REST ack.
 */
function settle(get: Get, set: Set, ack: Ack): void {
  const p = get().pending[ack.correlation_id];
  if (!p) return;
  if (!ack.ok) {
    fail(get, set, ack.correlation_id, ack.error?.message ?? `${p.label} didn't happen.`);
    return;
  }
  finishPending(get, set, ack.correlation_id);
  if (p.onFail !== "keep") toastNotes(ack, get().state?.sides);
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
