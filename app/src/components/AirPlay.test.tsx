/**
 * Phase 7 Pandora Sync (AirPlay bridge) UI: the picker affordance (station × capability × Sync
 * Play), the start flow through PlayerChrome, the status chip, Now Playing scoped to bridged rooms
 * (transport/seek off with one caption, other rooms live), the mini-player, and the Settings row
 * states plus the read-only outputs sheet.
 */
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { axe } from "vitest-axe";
import { PlayerChrome } from "./PlayerChrome";
import { ZonePicker } from "./ZonePicker";
import { NowPlaying } from "./NowPlaying";
import { MiniPlayer } from "./MiniPlayer";
import { PandoraSyncChip } from "./PandoraSyncChip";
import { SettingsScreen } from "./SettingsScreen";
import { useHub } from "@/lib/hub/store";
import { useLibrary, fresh } from "@/lib/library/store";
import { useChrome, type PlayRequest } from "@/lib/ui/chrome";
import { useToasts } from "@/lib/ui/toasts";
import { AIRPLAY_DISCLOSURE, AIRPLAY_ROW_TITLE, PANDORA_SYNC_BUTTON, PANDORA_SYNC_CHIP, PANDORA_SYNC_CONTROLLED, PANDORA_SYNC_STARTED } from "@/lib/airplay";
import { jsonResponse, okAck, sampleState, sampleSync } from "@/test/fixtures";
import type { AirPlayInfo, Settings } from "@/lib/hub/library";
import type { HubState } from "@/lib/hub/types";

vi.mock("next/navigation", () => ({ useRouter: () => ({ push: vi.fn(), back: vi.fn() }), useSearchParams: () => new URLSearchParams() }));
vi.mock("framer-motion", async () => {
  const actual = await vi.importActual<typeof import("framer-motion")>("framer-motion");
  return { ...actual, AnimatePresence: ({ children }: { children: React.ReactNode }) => <>{children}</> };
});

const art = { url: null, accent: null, accent_is_safe: false };
const stationReq: PlayRequest = { content_ref: { service: "pandora", kind: "station", id: "chill-radio" }, title: "Chill Radio", subtitle: null, art, preferred: [], availability: { heos: true, sonos: true } };
const albumReq: PlayRequest = { content_ref: { service: "tidal", kind: "album", id: "a-1" }, title: "Kind of Blue", subtitle: "Miles Davis", art, preferred: [], availability: { heos: true, sonos: true } };
const AVAILABLE: AirPlayInfo = { enabled: true, available: true, reason: null };
const OFF: AirPlayInfo = { enabled: false, available: false, reason: null };
const UNAVAILABLE: AirPlayInfo = { enabled: true, available: false, reason: "Automation permission for Music was denied. Allow it under Privacy & Security on the hub Mac." };
const outputs = [
  { id: "o1", name: "Kitchen", kind: "AirPlay device", kind_label: "AirPlay speaker", selected: true, active: false, available: true, volume: 30 },
  { id: "o2", name: "Living Room Amp", kind: "AirPlay device", kind_label: "AirPlay speaker", selected: true, active: true, available: true, volume: 40 },
  { id: "mac", name: "James's MacBook Pro", kind: "computer", kind_label: "This Mac", selected: false, active: false, available: true, volume: null },
];
const KITCHEN = "sonos:sonos-gK";
const LIVING = "heos:heos-1";

function settingsWith(airplay: AirPlayInfo): Settings {
  return { accounts: [], hardware: [], hub: { address: "10.0.0.5:8080", version: "0.4.0", uptime_s: 60, https: false, fake_devices: true, airplay } };
}
const bridgedState = (sideIds: string[] = [KITCHEN, LIVING]): HubState => ({
  ...sampleState(),
  pandora_sync: {
    active: true,
    side_ids: sideIds,
    output_ids: sideIds.map((id) => (id === KITCHEN ? "o1" : "o2")),
    outputs: sideIds.map((id) => (id === KITCHEN ? "Kitchen" : "Living Room Amp")),
    previous_output_ids: ["mac"],
    started_at: "2026-09-07T00:00:00Z",
    note: PANDORA_SYNC_STARTED,
  },
});

const fetchMock = vi.fn();
const fetcher = fetchMock as unknown as typeof fetch;
const urls = () => fetchMock.mock.calls.map((c) => String(c[0]).replace(/^https?:\/\/[^/]+/, ""));
const bodyOf = (path: string) => {
  const call = fetchMock.mock.calls.find((c) => String(c[0]).endsWith(path));
  return call?.[1]?.body ? JSON.parse(call[1].body as string) : undefined;
};

function boot(airplay: AirPlayInfo = AVAILABLE, state: HubState = sampleState(), opts: { settingsLoaded?: boolean } = {}) {
  localStorage.clear();
  useHub.getState()._reset();
  useToasts.setState({ toasts: [] });
  useChrome.setState({ npExpanded: false, zonesOpen: false, playRequest: null });
  useLibrary.getState()._reset();
  fetchMock.mockReset();
  fetchMock.mockImplementation(async (u: RequestInfo | URL, init?: RequestInit) => {
    const url = String(u).replace(/^https?:\/\/[^/]+/, "");
    if (url === "/api/airplay") return jsonResponse({ enabled: airplay.enabled, available: airplay.available, reason: airplay.reason, outputs: airplay.available ? outputs : [] });
    if (url === "/api/settings") return jsonResponse({ accounts: [], hardware: [], hub: { address: "10.0.0.5", port: 8080, https: false, version: "0.4.0", uptime_s: 60, fake_devices: true, airplay } });
    if (url.startsWith("/api/home")) return jsonResponse({ recents: [], playlists: { items: [], needs_link: [] }, favorite_albums: { items: [], needs_link: [] }, stations: { items: [], needs_link: [] } });
    return jsonResponse(okAck((init?.headers as Record<string, string> | undefined)?.["x-correlation-id"]));
  });
  useHub.setState({ _deps: { fetcher, now: () => Date.now(), setTimer: (fn, ms) => setTimeout(fn, ms), clearTimer: (t) => clearTimeout(t as ReturnType<typeof setTimeout>) } });
  useLibrary.setState({
    _deps: { fetcher, now: () => Date.now(), timeoutMs: 8000 },
    settings: opts.settingsLoaded === false ? fresh<Settings>() : { ...fresh<Settings>(), data: settingsWith(airplay), loadedAt: Date.now() },
  });
  useHub.getState().onMessage({ type: "snapshot", version: state.version, state });
  useHub.getState().setPhase("open", Date.now());
}
const flush = () => act(async () => {});

describe("Picker affordance: station × capability × Sync Play", () => {
  it.each([
    ["station + available", stationReq, AVAILABLE, true],
    ["station + off", stationReq, OFF, false],
    ["station + unavailable", stationReq, UNAVAILABLE, false],
    ["album + available", albumReq, AVAILABLE, false],
  ])("%s → shown: %s", async (_name, req, airplay, shown) => {
    boot(airplay);
    render(<ZonePicker open onClose={() => {}} play={req} onConfirm={() => {}} onPandoraSync={() => {}} />);
    await flush();
    const btn = screen.queryByTestId("pandora-sync");
    expect(!!btn).toBe(shown);
    if (btn) {
      expect(btn).toHaveTextContent(PANDORA_SYNC_BUTTON);
      // Plain text: amber is reserved for Sync Play.
      expect(btn.className).not.toContain("bg-signal");
      expect(btn.className).toContain("text-secondary");
    }
  });

  it("footer order: station note → confirm → Pandora Sync → its note (aria-describedby); disabled with no room; hands the selection over and closes; passes axe", async () => {
    boot();
    const onPandoraSync = vi.fn();
    const onClose = vi.fn();
    const { container } = render(<ZonePicker open onClose={onClose} play={{ ...stationReq, preferred: [KITCHEN] }} onConfirm={() => {}} onPandoraSync={onPandoraSync} />);
    await flush();
    const picker = screen.getByTestId("zone-picker");
    const order = Array.from(picker.querySelectorAll("[data-testid]"))
      .map((el) => el.getAttribute("data-testid"))
      .filter((t) => ["station-sync-note", "confirm-play", "pandora-sync", "pandora-sync-note"].includes(t!));
    expect(order).toEqual(["confirm-play", "pandora-sync", "pandora-sync-note"]);
    const btn = screen.getByTestId("pandora-sync");
    expect(btn).toBeEnabled();
    expect(btn).toHaveAttribute("aria-describedby", "pandora-sync-note");
    expect(screen.getByTestId("pandora-sync-note")).toHaveTextContent("The hub Mac plays this station to Kitchen + 1 over AirPlay. It opens Pandora there; press play on the Mac.");
    expect(await axe(container)).toHaveNoViolations();
    fireEvent.click(btn);
    expect(onPandoraSync).toHaveBeenCalledWith([KITCHEN]);
    expect(onClose).toHaveBeenCalled();
    // The first bridged room becomes the Now Playing side, so its chip shows in the mini-player.
    expect(useHub.getState().activeSideId).toBe(KITCHEN);
    expect(useHub.getState().selectedTargets).toEqual([KITCHEN]);
  });

  it("with both vendors selected on a station the note comes first, then confirm, then Pandora Sync", async () => {
    boot();
    render(<ZonePicker open onClose={() => {}} play={{ ...stationReq, preferred: [KITCHEN, LIVING] }} onConfirm={() => {}} onPandoraSync={() => {}} />);
    await flush();
    const picker = screen.getByTestId("zone-picker");
    const order = Array.from(picker.querySelectorAll("[data-testid]"))
      .map((el) => el.getAttribute("data-testid"))
      .filter((t) => ["station-sync-note", "confirm-play", "pandora-sync", "pandora-sync-note"].includes(t!));
    expect(order).toEqual(["station-sync-note", "confirm-play", "pandora-sync", "pandora-sync-note"]);
    expect(screen.getByTestId("station-sync-note")).toHaveTextContent("Stations can't Sync Play: Pandora picks different songs for each room.");
  });

  it("hides Pandora Sync while a Sync Play session is live, and hides Sync Play while Pandora Sync is on", async () => {
    boot(AVAILABLE, { ...sampleState(), sync: sampleSync() });
    const { unmount } = render(<ZonePicker open onClose={() => {}} play={{ ...stationReq, preferred: [KITCHEN] }} onConfirm={() => {}} onPandoraSync={() => {}} />);
    await flush();
    expect(screen.queryByTestId("pandora-sync")).toBeNull();
    unmount();
    boot(AVAILABLE, bridgedState());
    render(<ZonePicker open onClose={() => {}} play={{ ...albumReq, preferred: [KITCHEN, LIVING] }} onConfirm={() => {}} />);
    await flush();
    expect(screen.queryByTestId("confirm-play")).toHaveAttribute("data-mode", "play");
    expect(screen.queryByText("Sync Play")).toBeNull();
    expect(screen.getByTestId("sync-hidden-note")).toHaveTextContent("Pandora Sync is on. Stop it to use Sync Play.");
  });

  it("offers Pandora Sync after Settings load when the picker opened with no settings data (deep link, S4)", async () => {
    boot(AVAILABLE, sampleState(), { settingsLoaded: false });
    render(
      <PlayerChrome>
        <div />
      </PlayerChrome>,
    );
    act(() => useChrome.getState().requestPlay({ ...stationReq, preferred: [KITCHEN] }));
    await waitFor(() => expect(urls()).toContain("/api/settings"));
    expect(await screen.findByTestId("pandora-sync")).toBeVisible();
  });
});

describe("PlayerChrome start flow", () => {
  it("POSTs {side_ids} for every chosen room (no pre-flight, no client matching), toasts the hub's note, closes the picker", async () => {
    boot();
    render(
      <PlayerChrome>
        <div />
      </PlayerChrome>,
    );
    act(() => useChrome.getState().requestPlay({ ...stationReq, preferred: [KITCHEN, LIVING] }));
    await flush();
    fireEvent.click(await screen.findByTestId("pandora-sync"));
    await waitFor(() => expect(urls()).toContain("/api/pandora-sync/start"));
    expect(urls()).not.toContain("/api/airplay");
    expect(bodyOf("/api/pandora-sync/start")).toEqual({ side_ids: [KITCHEN, LIVING] });
    await waitFor(() => expect(useToasts.getState().toasts.map((t) => t.message)).toContain(PANDORA_SYNC_STARTED));
    expect(useChrome.getState().zonesOpen).toBe(false);
    expect(useChrome.getState().playRequest).toBeNull();
  });
});

describe("PandoraSyncChip", () => {
  it("hides when inactive; status only in both variants (no button, no live region, no pulse, no old strings)", () => {
    const { container, rerender } = render(<PandoraSyncChip state={{ active: false, side_ids: [], output_ids: [], outputs: [], previous_output_ids: [], started_at: null, note: null }} />);
    expect(container).toBeEmptyDOMElement();
    rerender(<PandoraSyncChip state={bridgedState().pandora_sync} />);
    const chip = screen.getByTestId("pandora-sync-chip");
    expect(chip).toHaveTextContent(PANDORA_SYNC_CHIP);
    expect(chip.tagName).toBe("SPAN");
    expect(chip.querySelector("svg[data-shape='airplay']")).not.toBeNull();
    expect(chip.querySelector(".pulse-once")).toBeNull();
    expect(chip.className).not.toContain("signal");
    expect(screen.queryByRole("button")).toBeNull();
    expect(screen.queryByRole("status")).toBeNull();
    rerender(<PandoraSyncChip state={bridgedState().pandora_sync} compact />);
    expect(screen.getByTestId("pandora-sync-chip")).toHaveAttribute("data-compact", "true");
    expect(document.body.textContent).not.toMatch(/via AirPlay/);
  });
});

describe("Now Playing while a room is bridged", () => {
  it("bridged room: status chip, 'Pandora Sync · 2 rooms' indicator naming the rooms, no badge, transport/±15/scrubber off with the caption tied by aria-describedby, outlined disc, room volume live, single Stop in error tone posts stop; passes axe", async () => {
    boot(AVAILABLE, bridgedState());
    useHub.getState().selectSide(KITCHEN);
    const { container } = render(<NowPlaying />);
    await flush();
    const np = screen.getByTestId("now-playing");
    expect(np).toHaveAttribute("data-np-bridged", "true");
    const chip = within(np).getByTestId("pandora-sync-chip");
    expect(chip).toHaveTextContent(PANDORA_SYNC_CHIP);
    expect(chip.tagName).toBe("SPAN");
    const indicator = within(np).getByTestId("target-indicator");
    expect(indicator).toHaveTextContent("Pandora Sync · 2 rooms");
    expect(indicator).toHaveAccessibleName(/Pandora Sync: Kitchen \+ 1 and Living Room Amp/);
    expect(indicator).not.toHaveAccessibleName(/rooms playing/);
    expect(within(np).queryByRole("img", { name: "Pandora" })).toBeNull();
    const caption = within(np).getByTestId("bridge-caption");
    expect(caption).toHaveTextContent(PANDORA_SYNC_CONTROLLED);
    expect(caption).toHaveAttribute("id", "bridge-caption");
    const toggle = within(np).getByTestId("play-toggle");
    expect(toggle).toBeDisabled();
    expect(toggle).toHaveAttribute("data-outlined", "true");
    expect(toggle).toHaveAttribute("aria-describedby", "bridge-caption");
    for (const name of ["Previous track", "Next track", "Back 15 seconds", "Forward 15 seconds"]) {
      const b = within(np).getByRole("button", { name });
      expect(b).toBeDisabled();
      expect(b).toHaveAttribute("aria-describedby", "bridge-caption");
    }
    expect(within(np).getByTestId("scrubber")).toHaveAttribute("data-seekable", "false");
    // The room's own volume keeps working; the volume sheet button stays live.
    expect(within(np).getByTestId("side-volume")).not.toHaveAttribute("aria-disabled");
    expect(within(np).getByTestId("open-volume")).toBeEnabled();
    // Exactly one Stop affordance, in the meta row, error tone.
    const stops = within(np).getAllByRole("button", { name: "Stop" });
    expect(stops).toHaveLength(1);
    expect(stops[0]!.className).toContain("text-error");
    expect(within(np).queryByTestId("stop-sync")).toBeNull();
    expect(within(np).queryByTestId("offer-sync")).toBeNull();
    fireEvent.click(stops[0]!);
    await waitFor(() => expect(urls()).toContain("/api/pandora-sync/stop"));
    expect(await axe(container)).toHaveNoViolations();
  });

  it("another room stays fully live: no chip, no caption, controls enabled (B3)", async () => {
    boot(AVAILABLE, bridgedState([KITCHEN]));
    useHub.getState().selectSide(LIVING);
    render(<NowPlaying />);
    await flush();
    const np = screen.getByTestId("now-playing");
    expect(np).not.toHaveAttribute("data-np-bridged");
    expect(within(np).queryByTestId("pandora-sync-chip")).toBeNull();
    expect(within(np).queryByTestId("bridge-caption")).toBeNull();
    expect(within(np).getByTestId("play-toggle")).toBeEnabled();
    expect(within(np).getByTestId("play-toggle")).not.toHaveAttribute("data-outlined");
    expect(within(np).getByRole("button", { name: "Next track" })).toBeEnabled();
    expect(within(np).queryByRole("button", { name: "Stop" })).toBeNull();
  });
});

describe("Mini-player while a room is bridged", () => {
  it("bridged room: non-interactive compact chip inside the expand button, play disabled; other room: no chip, play enabled", async () => {
    boot(AVAILABLE, bridgedState([KITCHEN]));
    useHub.getState().selectSide(KITCHEN);
    const onExpand = vi.fn();
    const { unmount } = render(<MiniPlayer onExpand={onExpand} />);
    await flush();
    const mini = screen.getByTestId("mini-player");
    const chip = within(mini).getByTestId("pandora-sync-chip");
    expect(chip).toHaveAttribute("data-compact", "true");
    expect(chip.tagName).toBe("SPAN");
    fireEvent.click(chip);
    expect(onExpand).toHaveBeenCalled();
    expect(within(mini).getByRole("button", { name: /^(Pause|Play)$/ })).toBeDisabled();
    unmount();
    boot(AVAILABLE, bridgedState([KITCHEN]));
    useHub.getState().selectSide(LIVING);
    render(<MiniPlayer onExpand={() => {}} />);
    await flush();
    expect(screen.queryByTestId("pandora-sync-chip")).toBeNull();
    expect(screen.getByRole("button", { name: /^(Pause|Play)$/ })).toBeEnabled();
  });

  it("buffering shows the pause icon (the device accepted a play)", async () => {
    const s = sampleState();
    s.sides[KITCHEN] = { ...s.sides[KITCHEN]!, play_state: "buffering" };
    boot(AVAILABLE, s);
    useHub.getState().selectSide(KITCHEN);
    render(<MiniPlayer onExpand={() => {}} />);
    await flush();
    expect(screen.getByRole("button", { name: "Pause" })).toHaveAttribute("aria-pressed", "true");
  });
});

describe("Settings: Pandora Sync (AirPlay bridge) row", () => {
  it.each([
    ["Off", OFF, "Off", false],
    ["Available", AVAILABLE, "Available", true],
    ["Unavailable", UNAVAILABLE, `Unavailable · ${UNAVAILABLE.reason}`, false],
  ])("%s: row and disclosure render; outputs row only when available", async (_n, airplay, line, hasOutputs) => {
    boot(airplay);
    render(<SettingsScreen />);
    const row = await screen.findByTestId("airplay-row");
    expect(row).toHaveTextContent(AIRPLAY_ROW_TITLE);
    expect(row).toHaveTextContent(line);
    // The load-bearing reason wraps rather than clamping (U9).
    expect(row.querySelector(".clamp-1.text-caption")).toBeNull();
    const disclosure = screen.getByTestId("airplay-disclosure");
    expect(disclosure).toHaveTextContent(AIRPLAY_DISCLOSURE);
    expect(disclosure.querySelector("p")!.className).toContain("text-caption");
    expect(disclosure.querySelector("p")!.className).toContain("text-secondary");
    expect(!!screen.queryByTestId("airplay-outputs")).toBe(hasOutputs);
  });

  it("the outputs sheet is read-only: neutral checks (no fill, no ring), kind labels in plain words, no controls; passes axe", async () => {
    boot();
    const { container } = render(<SettingsScreen />);
    fireEvent.click(await screen.findByTestId("airplay-outputs"));
    const sheet = await screen.findByTestId("airplay-outputs-sheet");
    await waitFor(() => expect(within(sheet).getAllByTestId("airplay-output")).toHaveLength(3));
    const rows = within(sheet).getAllByTestId("airplay-output");
    expect(rows.map((r) => r.getAttribute("data-selected"))).toEqual(["true", "true", "false"]);
    expect(rows[1]).toHaveTextContent("Living Room Amp");
    expect(rows[1]).toHaveTextContent("AirPlay speaker · Selected · Playing");
    expect(rows[2]).toHaveTextContent("This Mac");
    for (const r of rows) {
      expect(r.tagName).toBe("DIV");
      expect(r.innerHTML).not.toMatch(/bg-signal|border-signal/);
    }
    expect(within(sheet).queryAllByRole("button").filter((b) => !/close|done/i.test(b.textContent ?? "") && !b.getAttribute("aria-label")?.match(/close/i))).toHaveLength(0);
    expect(urls()).toContain("/api/airplay");
    expect(await axe(container)).toHaveNoViolations();
  });
});
