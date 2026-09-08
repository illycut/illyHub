import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { axe } from "vitest-axe";
import { SyncChip } from "./SyncChip";
import { ZonePicker, initialSelection } from "./ZonePicker";
import { NowPlaying } from "./NowPlaying";
import { MiniPlayer } from "./MiniPlayer";
import { PlayerChrome } from "./PlayerChrome";
import { useHub } from "@/lib/hub/store";
import { useChrome, type PlayRequest } from "@/lib/ui/chrome";
import { useToasts } from "@/lib/ui/toasts";
import { useLibrary, fresh } from "@/lib/library/store";
import { idleSync, jsonResponse, okAck, sampleState, sampleSync } from "@/test/fixtures";
import { SYNC_NOTE, SYNC_OFFER_NOTE, SYNC_UNSUPPORTED_TOAST } from "@/lib/sync";
import * as reducedMotion from "@/lib/reducedMotion";
import type { Detail, HistoryItem } from "@/lib/hub/library";

vi.mock("framer-motion", async () => {
  const actual = await vi.importActual<typeof import("framer-motion")>("framer-motion");
  return { ...actual, AnimatePresence: ({ children }: { children: React.ReactNode }) => <>{children}</> };
});

const fetchMock = vi.fn(async (_u: RequestInfo | URL, init?: RequestInit) =>
  jsonResponse(okAck((init?.headers as Record<string, string>)["x-correlation-id"])),
);
const fetcher = fetchMock as unknown as typeof fetch;

function boot(state = sampleState()) {
  useHub.getState()._reset();
  useToasts.setState({ toasts: [] });
  useChrome.setState({ npExpanded: false, zonesOpen: false, playRequest: null });
  useLibrary.getState()._reset?.();
  useHub.setState({ _deps: { fetcher, now: () => Date.now(), setTimer: (fn, ms) => setTimeout(fn, ms), clearTimer: (t) => clearTimeout(t as ReturnType<typeof setTimeout>) } });
  useHub.getState().onMessage({ type: "snapshot", version: state.version, state });
  useHub.getState().setPhase("open", Date.now());
  fetchMock.mockClear();
  localStorage.clear();
}

const lastBody = () => JSON.parse(fetchMock.mock.calls.at(-1)![1]!.body as string);
const lastUrl = () => String(fetchMock.mock.calls.at(-1)![0]).replace(/^https?:\/\/[^/]+/, "");
const urls = () => fetchMock.mock.calls.map((c) => String(c[0]).replace(/^https?:\/\/[^/]+/, ""));
const flush = () => act(async () => {});
const deltaSync = (sync: ReturnType<typeof sampleSync>, from = useHub.getState().state!.version) =>
  act(() => useHub.getState().onMessage({ type: "delta", from_version: from, to_version: from + 1, changed: { sync } }));
const glyphOf = (el: HTMLElement) => el.querySelector("svg[data-shape]")!;

const tidalReq: PlayRequest = {
  content_ref: { service: "tidal", kind: "album", id: "a-1" },
  title: "Kind of Blue",
  subtitle: "Miles Davis",
  art: { url: "/api/art/abc", accent: null, accent_is_safe: false },
  preferred: [],
  availability: { heos: true, sonos: true },
};
const pandoraReq: PlayRequest = { ...tidalReq, content_ref: { service: "pandora", kind: "station", id: "s-1" }, title: "Chill Station" };
const item = { content_ref: tidalReq.content_ref, title: tidalReq.title, subtitle: tidalReq.subtitle, art: tidalReq.art };

describe("SyncChip", () => {
  it("hides when idle/stopped; one SVG glyph carries shape + tone per status; no live region", () => {
    const { container, rerender } = render(<SyncChip sync={idleSync()} />);
    expect(container).toBeEmptyDOMElement();
    rerender(<SyncChip sync={sampleSync({ status: "stopped" })} />);
    expect(container).toBeEmptyDOMElement();
    for (const [status, text, shape, tone] of [
      ["resolving", "Starting", "dotted", "text-sync-drift"],
      ["priming", "Starting", "dotted", "text-sync-drift"],
      ["verifying", "Starting", "dotted", "text-sync-drift"],
      ["starting", "Starting", "dotted", "text-sync-drift"],
      ["locked", "Synced", "filled", "text-sync-locked"],
      ["drifting", "Adjusting", "half", "text-sync-drift"],
      ["correcting", "Adjusting", "half", "text-sync-drift"],
    ] as const) {
      rerender(<SyncChip sync={sampleSync({ status })} />);
      const chip = screen.getByTestId("sync-chip");
      expect(chip).toHaveTextContent(text);
      expect(chip).toHaveAttribute("data-status", status);
      expect(chip.querySelectorAll("svg[data-shape]")).toHaveLength(1);
      expect(glyphOf(chip)).toHaveAttribute("data-shape", shape);
      expect(glyphOf(chip).getAttribute("class")).toContain(tone);
      expect(screen.queryByRole("status")).toBeNull();
    }
  });
  it("pulses the glyph once per correction event (keyed on last_correction_at), never on mount, not under reduced motion", () => {
    const { rerender, unmount } = render(<SyncChip sync={sampleSync({ status: "correcting", last_correction_at: "2026-09-07T00:00:00Z" })} />);
    expect(glyphOf(screen.getByTestId("sync-chip")).getAttribute("class")).not.toContain("pulse-once");
    rerender(<SyncChip sync={sampleSync({ status: "correcting", last_correction_at: "2026-09-07T00:00:10Z" })} />);
    expect(glyphOf(screen.getByTestId("sync-chip")).getAttribute("class")).toContain("pulse-once");
    unmount();
    const spy = vi.spyOn(reducedMotion, "useReducedMotion").mockReturnValue(true);
    const r2 = render(<SyncChip sync={sampleSync({ status: "correcting", last_correction_at: "2026-09-07T00:00:00Z" })} />);
    r2.rerender(<SyncChip sync={sampleSync({ status: "correcting", last_correction_at: "2026-09-07T00:00:20Z" })} />);
    expect(glyphOf(screen.getByTestId("sync-chip")).getAttribute("class")).not.toContain("pulse-once");
    spy.mockRestore();
  });
  it("lost, full: chip 'Sync lost' (ring) + separate 48px Retry in text-primary + 'Stop sync', 8px apart; compact: one 'Sync lost · Retry' button; hidden drops it from the a11y tree", () => {
    const onRetry = vi.fn();
    const onStop = vi.fn();
    const { rerender } = render(<SyncChip sync={sampleSync({ status: "lost", reason: "sonos paused" })} onRetry={onRetry} onStop={onStop} />);
    const chip = screen.getByTestId("sync-chip");
    expect(chip.className).toContain("gap-gap-min");
    expect(glyphOf(chip)).toHaveAttribute("data-shape", "ring");
    const retry = screen.getByTestId("sync-retry");
    expect(retry).toHaveTextContent("Retry");
    expect(retry.className).toContain("text-primary");
    expect(retry.className).toContain("min-h-target");
    const stop = screen.getByTestId("sync-stop");
    expect(stop).toHaveTextContent("Stop sync");
    expect(stop.className).toContain("text-secondary");
    expect(stop.className).toContain("min-w-target");
    fireEvent.click(retry);
    fireEvent.click(stop);
    expect(onRetry).toHaveBeenCalledTimes(1);
    expect(onStop).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole("status")).toBeNull();
    rerender(<SyncChip sync={sampleSync({ status: "lost" })} onRetry={onRetry} onStop={onStop} compact />);
    expect(screen.queryByTestId("sync-stop")).toBeNull();
    expect(screen.getByTestId("sync-retry")).toHaveTextContent("Sync lost · Retry");
    rerender(<SyncChip sync={sampleSync({ status: "lost" })} onRetry={onRetry} compact hidden />);
    expect(screen.getByTestId("sync-retry")).toHaveAttribute("aria-hidden", "true");
    rerender(<SyncChip sync={sampleSync({ status: "locked" })} compact hidden />);
    expect(screen.getByTestId("sync-chip")).toHaveAttribute("aria-hidden", "true");
  });
});

describe("ZonePicker in play mode: Sync Play button rule", () => {
  beforeEach(() => boot());

  it("Tidal + HEOS + Sonos → amber Sync Play described by the room note, plain secondary Play 8px below; confirm reports both lists; passes axe", async () => {
    const onConfirm = vi.fn();
    const { container } = render(<ZonePicker open onClose={() => {}} play={tidalReq} onConfirm={onConfirm} />);
    const picker = screen.getByTestId("zone-picker");
    fireEvent.click(within(picker).getByTestId("zone-row-sonos:sonos-gK").querySelector("button")!);
    const confirm = within(picker).getByTestId("confirm-play");
    expect(confirm).toHaveTextContent("Sync Play");
    expect(confirm).toHaveAttribute("data-mode", "sync");
    expect(confirm).toHaveAttribute("aria-describedby", "sync-note");
    expect(confirm.className).toContain("bg-signal");
    expect(within(picker).getByTestId("sync-note")).toHaveTextContent(`Living Room Amp and Kitchen + 1, together. ${SYNC_NOTE}`);
    expect(within(picker).queryByTestId("sync-offer-note")).toBeNull();
    const plain = within(picker).getByTestId("confirm-play-plain");
    expect(plain).toHaveTextContent("Play on Kitchen + 1 and Living Room Amp");
    expect(plain.className).not.toContain("bg-signal");
    expect(plain.className).toContain("mt-gap-min");
    expect(await axe(container)).toHaveNoViolations();
    fireEvent.click(confirm);
    expect(onConfirm).toHaveBeenCalledWith(expect.arrayContaining(["heos:heos-1", "sonos:sonos-gK"]), { mode: "sync", heos: ["heos:heos-1"], sonos: ["sonos:sonos-gK"] });
    expect(useHub.getState().activeSideId).toBe("heos:heos-1");
  });

  it("launched from the Now Playing offer, the note gains the restart line", () => {
    render(<ZonePicker open onClose={() => {}} play={{ ...tidalReq, note: SYNC_OFFER_NOTE }} onConfirm={() => {}} />);
    fireEvent.click(screen.getByTestId("zone-row-sonos:sonos-gK").querySelector("button")!);
    expect(screen.getByTestId("sync-offer-note")).toHaveTextContent(SYNC_OFFER_NOTE);
  });

  it("the secondary plain button confirms an unsynced multi-room play", () => {
    const onConfirm = vi.fn();
    render(<ZonePicker open onClose={() => {}} play={tidalReq} onConfirm={onConfirm} />);
    fireEvent.click(screen.getByTestId("zone-row-sonos:sonos-gK").querySelector("button")!);
    fireEvent.click(screen.getByTestId("confirm-play-plain"));
    expect(onConfirm).toHaveBeenCalledWith(expect.any(Array), { mode: "play", syncReason: null });
  });

  it("Tidal + one vendor → plain Play, no Sync Play affordance at all", () => {
    render(<ZonePicker open onClose={() => {}} play={tidalReq} onConfirm={() => {}} />);
    const confirm = screen.getByTestId("confirm-play");
    expect(confirm).toHaveTextContent("Play on Living Room Amp");
    expect(confirm).toHaveAttribute("data-mode", "play");
    expect(screen.queryByTestId("sync-play-disabled")).toBeNull();
    expect(screen.queryByTestId("sync-note")).toBeNull();
    expect(screen.queryByTestId("confirm-play-plain")).toBeNull();
  });

  it("non-Tidal + both vendors → plain Play plus a disabled Sync Play with the reason (no silent fallback)", () => {
    const onConfirm = vi.fn();
    // a YouTube Music playlist: non-Tidal content that is not a station (stations get their own caption, Phase 5)
    const ytReq = { ...pandoraReq, content_ref: { service: "ytmusic" as const, kind: "playlist" as const, id: "p-1" }, title: "Focus" };
    render(<ZonePicker open onClose={() => {}} play={ytReq} onConfirm={onConfirm} />);
    fireEvent.click(screen.getByTestId("zone-row-sonos:sonos-gK").querySelector("button")!);
    const confirm = screen.getByTestId("confirm-play");
    expect(confirm).toHaveTextContent("Play on Kitchen + 1 and Living Room Amp");
    expect(confirm).toHaveAttribute("data-mode", "play");
    const disabled = screen.getByTestId("sync-play-disabled");
    expect(disabled).toBeDisabled();
    expect(disabled).toHaveTextContent("Sync Play");
    expect(disabled.className).not.toContain("bg-signal");
    expect(screen.getByTestId("sync-reason")).toHaveTextContent(SYNC_UNSUPPORTED_TOAST);
    fireEvent.click(confirm);
    expect(onConfirm).toHaveBeenCalledWith(expect.any(Array), { mode: "play", syncReason: SYNC_UNSUPPORTED_TOAST });
  });

  it("initialSelection skips offline sides", () => {
    const state = sampleState();
    state.players["heos-1"]!.online = false;
    expect(initialSelection({ ...tidalReq, preferred: ["heos:heos-1", "sonos:sonos-gK"] }, state, null)).toEqual(["sonos:sonos-gK"]);
    expect(initialSelection(tidalReq, state, "heos:heos-1")).toEqual([]);
  });
});

describe("store sync commands", () => {
  beforeEach(() => boot());

  it("syncPlay posts both targets (single id as string, several as a list) with X-Illyhub, marks all sides playing, sets sync resolving + title optimistically", async () => {
    const p = useHub.getState().syncPlay(["heos:heos-1"], ["sonos:sonos-gK"], item);
    const s = useHub.getState().state!;
    expect(s.sync).toMatchObject({ status: "resolving", master_side: "heos:heos-1", follower_side: "sonos:sonos-gK", title: "Kind of Blue" });
    expect(s.sides["sonos:sonos-gK"]!.play_state).toBe("play");
    expect(s.now_playing["sonos:sonos-gK"]).toMatchObject({ title: "Kind of Blue", seekable: false, content_ref: null });
    const ack = await p;
    expect(ack.ok).toBe(true);
    expect(lastUrl()).toBe("/api/sync/play");
    expect(lastBody()).toEqual({ content_ref: tidalReq.content_ref, heos_target: "heos:heos-1", sonos_target: "sonos:sonos-gK" });
    expect((fetchMock.mock.calls.at(-1)![1]!.headers as Record<string, string>)["X-Illyhub"]).toBe("1");
    await useHub.getState().syncPlay(["heos:heos-1", "heos:heos-2"], ["sonos:sonos-gK"], { ...item, start_index: 3 });
    expect(lastBody()).toEqual({ content_ref: tidalReq.content_ref, heos_target: ["heos:heos-1", "heos:heos-2"], sonos_target: "sonos:sonos-gK", start_index: 3 });
  });

  it("refusals toast in design voice and resync: unsupported_content, needs_link with Connect action, not_available_on_side naming the room, sync_mismatch", async () => {
    const resync = vi.fn();
    useHub.getState().setResync(resync);
    fetchMock.mockResolvedValueOnce(jsonResponse({ code: "unsupported_content", message: "Only Tidal content can Sync Play." }, 409));
    await useHub.getState().syncPlay(["heos:heos-1"], ["sonos:sonos-gK"], { ...item, content_ref: pandoraReq.content_ref });
    expect(useToasts.getState().toasts.at(-1)!.message).toBe(SYNC_UNSUPPORTED_TOAST);
    fetchMock.mockResolvedValueOnce(jsonResponse({ code: "needs_link", message: "Tidal is not linked." }, 409));
    await useHub.getState().syncPlay(["heos:heos-1"], ["sonos:sonos-gK"], item);
    expect(useToasts.getState().toasts.at(-1)).toMatchObject({ message: "Tidal is not linked.", action: { label: "Connect Tidal", href: "/settings?link=tidal" } });
    fetchMock.mockResolvedValueOnce(
      jsonResponse(okAck("c", { ok: false, error: { code: "not_available_on_side", message: "Kitchen + 1 can't play Tidal; link it in the Sonos app.", target: "sonos:sonos-gK", correlation_id: "c" } }), 409),
    );
    await useHub.getState().syncPlay(["heos:heos-1"], ["sonos:sonos-gK"], item);
    expect(useToasts.getState().toasts.at(-1)!.message).toContain("Kitchen + 1");
    fetchMock.mockResolvedValueOnce(jsonResponse({ code: "sync_mismatch", message: "The two sides did not load the same first track." }, 409));
    await useHub.getState().syncPlay(["heos:heos-1"], ["sonos:sonos-gK"], item);
    expect(useToasts.getState().toasts.at(-1)!.message).toBe("The two sides did not load the same first track.");
    expect(resync).toHaveBeenCalledTimes(4);
  });

  it("a sync delta arriving between the optimistic patch and the ack is kept (the ack does not roll it back)", async () => {
    let resolve!: (r: Response) => void;
    fetchMock.mockImplementationOnce(() => new Promise<Response>((r) => (resolve = r)));
    const p = useHub.getState().syncPlay(["heos:heos-1"], ["sonos:sonos-gK"], item);
    expect(useHub.getState().state!.sync.status).toBe("resolving");
    deltaSync(sampleSync({ status: "priming" }));
    expect(useHub.getState().state!.sync.status).toBe("priming");
    resolve(jsonResponse(okAck((fetchMock.mock.calls.at(-1)![1]!.headers as Record<string, string>)["x-correlation-id"])));
    await p;
    expect(useHub.getState().state!.sync.status).toBe("priming");
  });

  it("syncStop and syncRetry hit their endpoints with optimistic status; failures (sync_idle) toast and resync", async () => {
    deltaSync(sampleSync({ status: "lost" }));
    const r = useHub.getState().syncRetry();
    expect(useHub.getState().state!.sync.status).toBe("priming");
    await r;
    expect(lastUrl()).toBe("/api/sync/retry");
    const st = useHub.getState().syncStop();
    expect(useHub.getState().state!.sync.status).toBe("stopped");
    await st;
    expect(lastUrl()).toBe("/api/sync/stop");
    const resync = vi.fn();
    useHub.getState().setResync(resync);
    const errAck = (message: string) => async (_u: RequestInfo | URL, init?: RequestInit) => {
      const cid = (init?.headers as Record<string, string>)["x-correlation-id"];
      return jsonResponse(okAck(cid, { ok: false, error: { code: "sync_idle", message, target: null, correlation_id: cid } }), 409);
    };
    fetchMock.mockImplementationOnce(errAck("There is no Sync Play session to stop."));
    await useHub.getState().syncStop();
    expect(useToasts.getState().toasts.at(-1)!.message).toBe("There is no Sync Play session to stop.");
    fetchMock.mockImplementationOnce(errAck("Nothing to retry."));
    await useHub.getState().syncRetry();
    expect(useToasts.getState().toasts.at(-1)!.message).toBe("Nothing to retry.");
    expect(resync).toHaveBeenCalledTimes(2);
  });

  it("transport for either synced side routes to the master while mirrored; lost and stopped leave each side alone", async () => {
    deltaSync(sampleSync());
    await useHub.getState().transport("pause", "sonos:sonos-gK");
    expect(lastBody()).toEqual({ target: "heos:heos-1" });
    await useHub.getState().transport("next", "heos:heos-1");
    expect(lastBody()).toEqual({ target: "heos:heos-1" });
    deltaSync(sampleSync({ status: "lost" }));
    await useHub.getState().transport("pause", "sonos:sonos-gK");
    expect(lastBody()).toEqual({ target: "sonos:sonos-gK" });
    deltaSync(sampleSync({ status: "stopped" }));
    await useHub.getState().transport("pause", "sonos:sonos-gK");
    expect(lastBody()).toEqual({ target: "sonos:sonos-gK" });
  });
});

describe("Now Playing and mini-player during a session", () => {
  it("header says 'Syncing 2 rooms' with the full 'and' label in aria, chip Synced, read-only scrubber, both dots live, meta row reserves 48px, Stop sync posts /api/sync/stop; root carries data-np-sync", async () => {
    const state = sampleState();
    state.sync = sampleSync();
    boot(state);
    useHub.getState().selectSide("heos:heos-1");
    render(<NowPlaying />);
    const indicator = screen.getByTestId("target-indicator");
    expect(indicator).toHaveTextContent("Syncing 2 rooms");
    expect(indicator).not.toHaveTextContent("Living Room Amp");
    expect(indicator).toHaveAccessibleName(/Syncing Living Room Amp and Kitchen \+ 1\. 2 rooms playing/);
    expect(indicator.className).toContain("min-w-0");
    expect(indicator.querySelector(".truncate")).not.toBeNull();
    expect(screen.getByTestId("sync-chip")).toHaveTextContent("Synced");
    expect(screen.getByTestId("sync-chip").className).toContain("shrink-0");
    expect(screen.getByTestId("now-playing")).toHaveAttribute("data-np-sync", "true");
    expect(screen.getByTestId("stop-sync").parentElement!.className).toContain("min-h-target");
    expect(screen.getByRole("slider", { name: /position|progress|seek/i })).toHaveAttribute("aria-readonly", "true");
    expect(screen.queryByTestId("offer-sync")).toBeNull();
    expect(indicator.querySelectorAll('[data-live="true"]')).toHaveLength(2);
    fireEvent.click(screen.getByTestId("stop-sync"));
    await flush();
    expect(lastUrl()).toBe("/api/sync/stop");
    expect(indicator).toHaveTextContent("Living Room Amp");
    expect(screen.queryByTestId("sync-chip")).toBeNull();
    // right after the stop the offer is suppressed for the rooms of that session (S5)
    expect(screen.queryByTestId("offer-sync")).toBeNull();
    expect(screen.getByTestId("now-playing")).not.toHaveAttribute("data-np-sync");
  });

  it("lost: chip area shows 'Sync lost' + Retry + Stop sync (no second Stop sync in the meta row), dots stop claiming both live, retry posts /api/sync/retry from Now Playing and the compact mini-player button", async () => {
    const state = sampleState();
    state.sync = sampleSync({ status: "lost", reason: "sonos paused" });
    boot(state);
    useHub.getState().selectSide("heos:heos-1");
    render(<NowPlaying />);
    expect(screen.getByTestId("sync-chip")).toHaveTextContent("Sync lost");
    expect(screen.getByTestId("sync-stop")).toHaveTextContent("Stop sync");
    expect(screen.getByTestId("sync-retry")).toHaveTextContent("Retry");
    expect(screen.queryByTestId("stop-sync")).toBeNull();
    expect(screen.getByTestId("target-indicator").querySelectorAll('[data-live="true"]')).toHaveLength(1);
    fireEvent.click(screen.getByTestId("sync-retry"));
    await flush();
    expect(lastUrl()).toBe("/api/sync/retry");
    fetchMock.mockClear();
    deltaSync(sampleSync({ status: "lost" }));
    render(<MiniPlayer onExpand={() => {}} />);
    const retries = screen.getAllByTestId("sync-retry");
    expect(retries).toHaveLength(2);
    expect(retries.at(-1)).toHaveTextContent("Sync lost · Retry");
    fireEvent.click(retries.at(-1)!);
    await flush();
    expect(lastUrl()).toBe("/api/sync/retry");
  });

  it("mini-player chip is hidden from the a11y tree while Now Playing is expanded", () => {
    const state = sampleState();
    state.sync = sampleSync();
    boot(state);
    render(<MiniPlayer onExpand={() => {}} hideArt />);
    expect(screen.getByTestId("sync-chip")).toHaveAttribute("aria-hidden", "true");
  });

  it("offer: from the canonical content_ref (either vendor), sits by the badge, opens the picker with both pre-selected, the restart note, and no hardcoded availability; bare track when no container matches", () => {
    boot();
    useHub.getState().selectSide("heos:heos-1"); // Tidal track with content_ref in the fixture
    render(<NowPlaying />);
    const offer = screen.getByTestId("offer-sync");
    expect(offer).toHaveAccessibleName("Sync Play with Kitchen + 1");
    expect(offer.className).not.toContain("ml-auto");
    fireEvent.click(offer);
    const req = useChrome.getState().playRequest!;
    expect(useChrome.getState().zonesOpen).toBe(true);
    expect(req.content_ref).toEqual({ service: "tidal", kind: "track", id: "1" });
    expect(req.start_index).toBeUndefined();
    expect(req.preferred).toEqual(["heos:heos-1", "sonos:sonos-gK"]);
    expect(req.availability).toBeUndefined();
    expect(req.note).toBe(SYNC_OFFER_NOTE);
    expect(req.title).toBe("Blue in Green");
    const state = sampleState();
    state.now_playing["sonos:sonos-gK"] = { ...state.now_playing["heos:heos-1"]!, content_ref: { service: "tidal", kind: "track", id: "9" } };
    boot(state);
    useHub.getState().selectSide("sonos:sonos-gK");
    render(<NowPlaying />);
    expect(screen.getAllByTestId("offer-sync").at(-1)).toHaveAccessibleName("Sync Play with Living Room Amp");
  });

  it("offer prefers the room's recent album when its cached detail holds the current track, starting there", () => {
    boot();
    const art = { url: null, accent: null, accent_is_safe: false };
    const album: HistoryItem = { content_ref: { service: "tidal", kind: "album", id: "a-1" }, title: "Kind of Blue", subtitle: "Miles Davis", art, last_targets: ["heos:heos-1"], last_played_at: "2026-09-07T00:00:00Z", play_count: 1, availability: null };
    const detail: Detail = {
      item: { content_ref: album.content_ref, title: "Kind of Blue", subtitle: "Miles Davis", art } as Detail["item"],
      tracks: [
        { content_ref: { service: "tidal", kind: "track", id: "0" }, title: "So What", subtitle: null, art, index: 0, artist: null, album: null },
        { content_ref: { service: "tidal", kind: "track", id: "1" }, title: "Blue in Green", subtitle: null, art, index: 1, artist: null, album: null },
      ] as Detail["tracks"],
    };
    useLibrary.setState({
      home: { ...fresh(), data: { recents: { items: [album], needs_link: null, error: null }, playlists: { items: [], needs_link: null, error: null }, favorite_albums: { items: [], needs_link: null, error: null }, stations: { items: [], needs_link: null, error: null } } } as never,
      details: { "tidal:album:a-1": { ...fresh(), data: detail } } as never,
    });
    useHub.getState().selectSide("heos:heos-1");
    render(<NowPlaying />);
    fireEvent.click(screen.getByTestId("offer-sync"));
    const req = useChrome.getState().playRequest!;
    expect(req.content_ref).toEqual(album.content_ref);
    expect(req.start_index).toBe(1);
    expect(req.title).toBe("Kind of Blue");
  });

  it("no offer when content_ref is null (a Tidal-looking track_id is never enough), nor for non-Tidal", () => {
    const state = sampleState();
    state.now_playing["heos:heos-1"]!.content_ref = null;
    state.now_playing["heos:heos-1"]!.track_id = "tidal:1";
    boot(state);
    useHub.getState().selectSide("heos:heos-1");
    render(<NowPlaying />);
    expect(screen.queryByTestId("offer-sync")).toBeNull();
    expect(screen.getByTestId("now-playing")).not.toHaveAttribute("data-np-sync");
    boot();
    useHub.getState().selectSide("sonos:sonos-gK"); // Pandora
    render(<NowPlaying />);
    expect(screen.queryByTestId("offer-sync")).toBeNull();
    expect(screen.queryByTestId("stop-sync")).toBeNull();
  });
});

describe("PlayerChrome", () => {
  it("confirm in sync mode calls /api/sync/play once (no /api/play) and refreshes home", async () => {
    boot();
    const invalidate = vi.fn();
    useLibrary.setState({ invalidateHome: invalidate } as never);
    render(
      <PlayerChrome>
        <div />
      </PlayerChrome>,
    );
    act(() => useChrome.getState().requestPlay(tidalReq));
    const picker = screen.getByTestId("zone-picker");
    fireEvent.click(within(picker).getByTestId("zone-row-sonos:sonos-gK").querySelector("button")!);
    fireEvent.click(within(picker).getByTestId("confirm-play"));
    await flush();
    expect(urls().filter((u) => u === "/api/sync/play")).toHaveLength(1);
    expect(urls().filter((u) => u === "/api/play")).toHaveLength(0);
    expect(invalidate).toHaveBeenCalled();
  });

  it("owns the single polite announcer: mounted before any session, fires per TEXT change (Starting, Synced, Adjusting once for drifting+correcting, Sync lost), and no chip carries a live region", () => {
    boot();
    render(
      <PlayerChrome>
        <div />
      </PlayerChrome>,
    );
    const announcer = screen.getByTestId("sync-announcer");
    expect(announcer).toHaveAttribute("aria-live", "polite");
    expect(announcer).toHaveTextContent("");
    expect(screen.getAllByRole("status")).toHaveLength(1);
    deltaSync(sampleSync({ status: "resolving" }));
    expect(announcer).toHaveTextContent("Starting");
    deltaSync(sampleSync({ status: "locked" }));
    expect(announcer).toHaveTextContent("Synced");
    deltaSync(sampleSync({ status: "drifting" }));
    expect(announcer).toHaveTextContent("Adjusting");
    deltaSync(sampleSync({ status: "correcting", last_correction_at: "2026-09-07T00:00:01Z" }));
    expect(announcer).toHaveTextContent("Adjusting");
    deltaSync(sampleSync({ status: "lost", reason: "sonos paused" }));
    expect(announcer).toHaveTextContent("Sync lost");
    expect(screen.getAllByRole("status")).toHaveLength(1);
    deltaSync(sampleSync({ status: "stopped" }));
    expect(announcer).toHaveTextContent("");
  });
});
