/**
 * Phase 7 Pandora Sync (AirPlay bridge) view logic, normalisers, delta handling, the store
 * start/stop flows, and the buffering play-state rules (S11).
 */
import messages from "./hub/messages.json";
import {
  AIRPLAY_DISCLOSURE,
  AIRPLAY_ROW_TITLE,
  PANDORA_SYNC,
  PANDORA_SYNC_BUTTON,
  PANDORA_SYNC_CHIP,
  PANDORA_SYNC_CONTROLLED,
  PANDORA_SYNC_STARTED,
  PANDORA_SYNC_STOPPED,
  airplayRowStatus,
  bridgedSide,
  isPandoraSyncActive,
  pandoraSyncLabel,
  pandoraSyncLabelShort,
  pandoraSyncNote,
  pandoraSyncOffered,
} from "./airplay";
import { asAirPlayInfo, asAirPlayStatus, asSettings } from "./hub/library";
import { applyDelta, asPandoraSync, emptyPandoraSync, emptyState } from "./hub/state";
import { useHub } from "./hub/store";
import { useToasts } from "./ui/toasts";
import { isPlaying, isPlayingState, isSettledState } from "./playState";
import { interpolatePosition } from "./position";
import { liveSideIds, resolveActiveSide } from "./selectors";
import { jsonResponse, okAck, sampleState } from "@/test/fixtures";
import type { AirPlayOutput, HubState } from "./hub/types";

const out = (id: string, name: string, over: Partial<AirPlayOutput> = {}): AirPlayOutput => ({ id, name, kind: "AirPlay device", kind_label: "AirPlay speaker", selected: false, active: false, available: true, volume: null, ...over });
const ps = (over: Partial<NonNullable<HubState["pandora_sync"]>> = {}): NonNullable<HubState["pandora_sync"]> => ({
  active: true,
  side_ids: ["sonos:sonos-gK", "heos:heos-1"],
  output_ids: ["o1", "o2"],
  outputs: ["Kitchen", "Living Room Amp"],
  previous_output_ids: ["mac"],
  started_at: null,
  note: null,
  ...over,
});

describe("copy", () => {
  it("uses one name everywhere (Pandora Sync), sentence case, never perfect, no old strings", () => {
    for (const s of [PANDORA_SYNC_BUTTON, PANDORA_SYNC_CHIP, PANDORA_SYNC_CONTROLLED, PANDORA_SYNC_STARTED, PANDORA_SYNC_STOPPED, AIRPLAY_ROW_TITLE, AIRPLAY_DISCLOSURE]) {
      expect(s).not.toMatch(/perfect/i);
      expect(s).not.toMatch(/Sync via AirPlay|Pandora via AirPlay/);
      expect(s[0]).toBe(s[0]!.toUpperCase());
    }
    expect(PANDORA_SYNC_BUTTON).toBe("Pandora Sync (experimental)");
    expect(PANDORA_SYNC_BUTTON.match(/experimental/g)).toHaveLength(1);
    expect(PANDORA_SYNC_CHIP).toBe(PANDORA_SYNC);
    expect(AIRPLAY_ROW_TITLE).toBe("Pandora Sync (AirPlay bridge)");
    expect(pandoraSyncNote(["Kitchen", "Living Room Amp"])).toBe("The hub Mac plays this station to Kitchen and Living Room Amp over AirPlay. It opens Pandora there; press play on the Mac.");
    expect(pandoraSyncNote([])).toContain("your rooms");
  });
  it("reads the started/stopped notes from the hub's exported templates.airplay", () => {
    const airplay = (messages as { templates: { airplay?: Record<string, string> } }).templates.airplay;
    expect(airplay?.pandora_sync_started).toBeTruthy();
    expect(PANDORA_SYNC_STARTED).toBe(airplay!.pandora_sync_started);
    if (airplay?.pandora_sync_stopped) expect(PANDORA_SYNC_STOPPED).toBe(airplay.pandora_sync_stopped);
  });
});

describe("pandoraSyncOffered (station × capability × Sync Play)", () => {
  const station = { service: "pandora", kind: "station" };
  const avail = { enabled: true, available: true, reason: null };
  it.each([
    [station, avail, false, true],
    [station, avail, true, false],
    [station, { enabled: true, available: false, reason: "Music app is not running" }, false, false],
    [station, { enabled: false, available: false, reason: null }, false, false],
    [station, null, false, false],
    [{ service: "tidal", kind: "album" }, avail, false, false],
    [{ service: "pandora", kind: "track" }, avail, false, false],
    [{ service: "ytmusic", kind: "playlist" }, avail, false, false],
    [null, avail, false, false],
  ])("%j × %j × syncActive=%s → %s", (ref, airplay, syncActive, expected) => {
    expect(pandoraSyncOffered(ref, airplay, syncActive)).toBe(expected);
  });
});

describe("airplayRowStatus", () => {
  it("reads Off / Available / Unavailable · reason", () => {
    expect(airplayRowStatus({ enabled: false, available: false, reason: null })).toBe("Off");
    expect(airplayRowStatus({ enabled: true, available: true, reason: null })).toBe("Available");
    expect(airplayRowStatus({ enabled: true, available: false, reason: "Automation permission for Music was denied." })).toBe("Unavailable · Automation permission for Music was denied.");
    expect(airplayRowStatus({ enabled: true, available: false, reason: "  " })).toBe("Unavailable · The Music app didn't answer.");
    expect(airplayRowStatus(null)).toBe("Off");
  });
});

describe("bridgedSide and labels", () => {
  const bridged = (over: Partial<NonNullable<HubState["pandora_sync"]>> = {}): HubState => ({ ...sampleState(), pandora_sync: ps(over) });
  it("scopes to side_ids; an active hub without side_ids bridges every room; inactive bridges none", () => {
    const one = ps({ side_ids: ["sonos:sonos-gK"], outputs: ["Kitchen"], output_ids: ["o1"] });
    expect(bridgedSide("sonos:sonos-gK", one)).toBe(true);
    expect(bridgedSide("heos:heos-1", one)).toBe(false);
    expect(bridgedSide("heos:heos-1", ps({ side_ids: [] }))).toBe(true);
    expect(bridgedSide("heos:heos-1", ps({ active: false }))).toBe(false);
    expect(bridgedSide(null, ps())).toBe(false);
    expect(bridgedSide("x", null)).toBe(false);
  });
  it("is inactive with no field, an empty field, or active false", () => {
    const s = sampleState();
    delete s.pandora_sync;
    expect(isPandoraSyncActive(s)).toBe(false);
    expect(isPandoraSyncActive({ ...s, pandora_sync: emptyPandoraSync() })).toBe(false);
    expect(isPandoraSyncActive(null)).toBe(false);
    expect(pandoraSyncLabel(s)).toBeNull();
    expect(pandoraSyncLabelShort(s)).toBeNull();
  });
  it("names the bridged rooms from the hub's sides (else output names), joined with 'and'; the short form counts rooms", () => {
    const st = bridged();
    expect(isPandoraSyncActive(st)).toBe(true);
    expect(pandoraSyncLabel(st, st.sides)).toBe("Pandora Sync: Kitchen + 1 and Living Room Amp");
    expect(pandoraSyncLabel(st)).toBe("Pandora Sync: Kitchen and Living Room Amp");
    expect(pandoraSyncLabelShort(st)).toBe("Pandora Sync · 2 rooms");
    expect(pandoraSyncLabelShort(bridged({ side_ids: ["sonos:sonos-gK"] }))).toBe("Pandora Sync · 1 room");
    expect(pandoraSyncLabelShort(bridged({ side_ids: [], outputs: [], output_ids: [] }))).toBe("Pandora Sync");
  });
});

describe("normalisers", () => {
  it("hub.airplay defaults to off; available requires enabled; asAirPlayStatus takes enabled from the payload only", () => {
    expect(asAirPlayInfo(undefined)).toEqual({ enabled: false, available: false, reason: null });
    expect(asAirPlayInfo({ enabled: false, available: true })).toEqual({ enabled: false, available: false, reason: null });
    const status = asAirPlayStatus({
      enabled: true,
      available: true,
      reason: null,
      outputs: [{ id: "7", name: "Kitchen", kind: "AirPlay device", kind_label: "AirPlay speaker", selected: true, active: false, available: true, volume: 40 }, { name: "Den" }],
    });
    expect(status.enabled).toBe(true);
    expect(status.outputs).toEqual([
      { id: "7", name: "Kitchen", kind: "AirPlay device", kind_label: "AirPlay speaker", selected: true, active: false, available: true, volume: 40 },
      { id: "Den", name: "Den", kind: null, kind_label: null, selected: false, active: false, available: true, volume: null },
    ]);
    // enabled missing → off, even with outputs present
    expect(asAirPlayStatus({ available: true, reason: null, outputs: [{ id: "1", name: "X" }] }).enabled).toBe(false);
    expect(asAirPlayStatus({ enabled: true, available: false, reason: "Music is not running", outputs: [] })).toEqual({ enabled: true, available: false, reason: "Music is not running", outputs: [] });
  });
  it("settings carry hub.airplay and default it when the hub predates the bridge", () => {
    const base = { accounts: [], hardware: [], hub: { address: "10.0.0.5", port: 8080, https: false, version: "0.4.0", uptime_s: 1, fake_devices: false } };
    expect(asSettings(base).hub.airplay).toEqual({ enabled: false, available: false, reason: null });
    expect(asSettings({ ...base, hub: { ...base.hub, airplay: { enabled: true, available: false, reason: "x" } } }).hub.airplay).toEqual({ enabled: true, available: false, reason: "x" });
  });
  it("asPandoraSync coerces every field and reads garbage as inactive", () => {
    expect(asPandoraSync(null)).toEqual(emptyPandoraSync());
    expect(asPandoraSync({ active: true, side_ids: ["a", 3], outputs: ["Kitchen"], note: 7 })).toEqual({ ...emptyPandoraSync(), active: true, side_ids: ["a"], outputs: ["Kitchen"] });
  });
});

describe("delta handling", () => {
  it("applies pandora_sync as a depth-one path and treats null as inactive", () => {
    const s = emptyState();
    const active = ps({ started_at: "2026-09-07T00:00:00Z", note: "Press play on the Mac." });
    const next = applyDelta(s, { type: "delta", from_version: 0, to_version: 1, changed: { pandora_sync: active } })!;
    expect(next.pandora_sync).toEqual(active);
    const gone = applyDelta(next, { type: "delta", from_version: 1, to_version: 2, changed: { pandora_sync: null } })!;
    expect(gone.pandora_sync).toEqual(emptyPandoraSync());
  });
});

describe("store: pandoraSyncStart / pandoraSyncStop", () => {
  const fetchMock = vi.fn();
  const fetcher = fetchMock as unknown as typeof fetch;
  const calls = () => fetchMock.mock.calls.map((c) => [String(c[0]).replace(/^https?:\/\/[^/]+/, ""), c[1]?.body ? JSON.parse(c[1].body as string) : undefined]);
  const messagesShown = () => useToasts.getState().toasts.map((t) => t.message);
  const ackFor = (init?: RequestInit, over: Record<string, unknown> = {}) => jsonResponse(okAck((init?.headers as Record<string, string>)["x-correlation-id"], over));
  beforeEach(() => {
    useHub.getState()._reset();
    useToasts.setState({ toasts: [] });
    useHub.setState({ _deps: { fetcher, now: () => 1_000, setTimer: () => 0, clearTimer: () => {} } });
    useHub.getState().onMessage({ type: "snapshot", version: 10, state: sampleState() });
    fetchMock.mockReset();
  });

  it("start: POSTs {side_ids} with no optimistic state; toasts the hub's started note once", async () => {
    fetchMock.mockImplementation(async (_u: RequestInfo | URL, init?: RequestInit) => ackFor(init));
    const p = useHub.getState().pandoraSyncStart(["sonos:sonos-gK", "heos:heos-1"]);
    // No optimistic active: the hub's delta drives the chip.
    expect(useHub.getState().state?.pandora_sync?.active).toBe(false);
    const ack = await p;
    expect(ack.ok).toBe(true);
    expect(calls()).toEqual([["/api/pandora-sync/start", { side_ids: ["sonos:sonos-gK", "heos:heos-1"] }]]);
    expect(messagesShown()).toEqual([PANDORA_SYNC_STARTED]);
  });

  it("start: the note comes from the state once the delta landed; a hub warning is toasted alongside", async () => {
    fetchMock.mockImplementation(async (_u: RequestInfo | URL, init?: RequestInit) => {
      // The delta lands before the REST ack is read, as on the wire.
      useHub.getState().onMessage({ type: "delta", from_version: 10, to_version: 11, changed: { pandora_sync: ps({ note: "Press play on the Mac now." }) } });
      return ackFor(init, { warnings: [{ code: "airplay_restore_failed", message: "Couldn't remember the Mac's previous outputs.", target: null }] });
    });
    await useHub.getState().pandoraSyncStart(["sonos:sonos-gK"]);
    expect(messagesShown()).toEqual(["Couldn't remember the Mac's previous outputs.", "Press play on the Mac now."]);
  });

  it.each([
    ["bridge_unavailable", 503, "AirPlay isn't available on the hub right now: Music is not running."],
    ["invalid_argument", 400, "No AirPlay outputs match Kitchen + 1. Pick the outputs by hand."],
    ["sync_active", 409, "Stop Sync Play before starting Pandora Sync."],
  ])("start refused with %s (%s): the hub's sentence is toasted verbatim and the optimistic layer is dropped via resync", async (code, status, message) => {
    const resync = vi.fn();
    useHub.getState().setResync(resync);
    fetchMock.mockImplementation(async () => jsonResponse({ code, message }, status));
    const ack = await useHub.getState().pandoraSyncStart(["sonos:sonos-gK"]);
    expect(ack.ok).toBe(false);
    expect(messagesShown()).toEqual([message]);
    expect(resync).toHaveBeenCalledTimes(1);
  });

  it("stop: optimistic inactive, POSTs /api/pandora-sync/stop, toasts the stopped note once", async () => {
    useHub.setState((s) => ({ state: { ...s.state!, pandora_sync: ps() } }));
    fetchMock.mockImplementation(async (_u: RequestInfo | URL, init?: RequestInit) => ackFor(init));
    const p = useHub.getState().pandoraSyncStop();
    expect(useHub.getState().state?.pandora_sync?.active).toBe(false);
    await p;
    expect(calls()).toEqual([["/api/pandora-sync/stop", {}]]);
    expect(messagesShown()).toEqual([PANDORA_SYNC_STOPPED]);
  });

  it("stop while idle: the hub's sync_idle is not an error toast; other refusals are said verbatim", async () => {
    const resync = vi.fn();
    useHub.getState().setResync(resync);
    fetchMock.mockImplementation(async () => jsonResponse({ code: "sync_idle", message: "Pandora Sync isn't running." }, 409));
    await useHub.getState().pandoraSyncStop();
    expect(messagesShown()).toEqual([]);
    expect(resync).toHaveBeenCalledTimes(1);
    fetchMock.mockImplementation(async () => jsonResponse({ code: "bridge_unavailable", message: "AirPlay isn't available on the hub right now: the Music app quit." }, 503));
    await useHub.getState().pandoraSyncStop();
    expect(messagesShown()).toEqual(["AirPlay isn't available on the hub right now: the Music app quit."]);
  });
});

describe("buffering (S11)", () => {
  it("one predicate: play and buffering count as playing; pause/stop/unknown do not; settled = play/pause/stop", () => {
    expect(isPlayingState("play")).toBe(true);
    expect(isPlayingState("buffering")).toBe(true);
    for (const s of ["pause", "stop", "unknown", undefined] as const) expect(isPlayingState(s)).toBe(false);
    expect(isPlaying({ play_state: "buffering" })).toBe(true);
    expect(isSettledState("buffering")).toBe(false);
    expect(isSettledState("pause")).toBe(true);
  });
  it("selectors and interpolation treat a buffering side as live", () => {
    const s = sampleState();
    s.sides["sonos:sonos-gK"] = { ...s.sides["sonos:sonos-gK"]!, play_state: "buffering" };
    expect(liveSideIds(s)).toEqual(expect.arrayContaining(["sonos:sonos-gK", "heos:heos-1"]));
    s.sides["heos:heos-1"] = { ...s.sides["heos:heos-1"]!, play_state: "pause" };
    // Kitchen has now-playing metadata and is buffering → it is the active side
    expect(resolveActiveSide(s, null)?.id).toBe("sonos:sonos-gK");
    const pos = { position_ms: 1000, reported_at: new Date(0).toISOString(), confidence: 1 };
    expect(interpolatePosition(pos, "buffering", 2500)).toBe(3500);
    expect(interpolatePosition(pos, "pause", 2500)).toBe(1000);
  });
  it("store: the optimistic intent is held while the hub reports buffering, cleared once it settles; toggle from buffering sends pause", async () => {
    const fetchMock = vi.fn(async (_u: RequestInfo | URL, init?: RequestInit) => jsonResponse(okAck((init?.headers as Record<string, string>)["x-correlation-id"])));
    useHub.getState()._reset();
    useHub.setState({ _deps: { fetcher: fetchMock as unknown as typeof fetch, now: () => 1_000, setTimer: () => 0, clearTimer: () => {} } });
    const st = sampleState();
    st.sides["sonos:sonos-gK"] = { ...st.sides["sonos:sonos-gK"]!, play_state: "stop" };
    useHub.getState().onMessage({ type: "snapshot", version: 10, state: st });
    await useHub.getState().transport("play", "sonos:sonos-gK");
    expect(useHub.getState().displayPlayState("sonos:sonos-gK")).toBe("play");
    // The hub reports buffering: the icon must keep showing what the user asked for.
    useHub.getState().onMessage({ type: "delta", from_version: 10, to_version: 11, changed: { "sides.sonos:sonos-gK": { ...st.sides["sonos:sonos-gK"], play_state: "buffering" } } });
    expect(useHub.getState().state?.sides["sonos:sonos-gK"]?.play_state).toBe("buffering");
    expect(useHub.getState().displayPlayState("sonos:sonos-gK")).toBe("play");
    // A toggle while buffering is sent as an explicit pause.
    fetchMock.mockClear();
    await useHub.getState().transport("toggle", "sonos:sonos-gK");
    expect(String(fetchMock.mock.calls[0]![0])).toContain("/api/transport/pause");
    expect(useHub.getState().displayPlayState("sonos:sonos-gK")).toBe("pause");
    // Settled state from the hub clears the held intent.
    useHub.getState().onMessage({ type: "delta", from_version: 11, to_version: 12, changed: { "sides.sonos:sonos-gK": { ...st.sides["sonos:sonos-gK"], play_state: "play" } } });
    expect(useHub.getState().intents["sonos:sonos-gK"]).toBeUndefined();
    expect(useHub.getState().displayPlayState("sonos:sonos-gK")).toBe("play");
  });
});

describe("outputs", () => {
  it("keep the hub's kind_label for display", () => {
    expect(out("1", "Mac", { kind: "computer", kind_label: "This Mac" }).kind_label).toBe("This Mac");
  });
});
