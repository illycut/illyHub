import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { axe } from "vitest-axe";
import { NowPlaying } from "./NowPlaying";
import { MiniPlayer, miniPlayerRenders, resetMiniPlayerRenders } from "./MiniPlayer";
import { VolumeSheet } from "./VolumeSheet";
import { ZonePicker, sideStatus } from "./ZonePicker";
import { PlayerChrome, shouldCollapse } from "./PlayerChrome";
import { useChrome } from "@/lib/ui/chrome";
import { readLastTarget, rememberTargets } from "@/lib/prefs";
import { useLibrary } from "@/lib/library/store";
import { useHub } from "@/lib/hub/store";
import { useToasts } from "@/lib/ui/toasts";
import { jsonResponse, okAck, sampleState, side } from "@/test/fixtures";

// jsdom never finishes framer-motion exit animations; make AnimatePresence unmount immediately.
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
  useHub.setState({ _deps: { fetcher, now: () => Date.now(), setTimer: (fn, ms) => setTimeout(fn, ms), clearTimer: (t) => clearTimeout(t as ReturnType<typeof setTimeout>) } });
  useHub.getState().onMessage({ type: "snapshot", version: state.version, state });
  useHub.getState().setPhase("open", Date.now());
  fetchMock.mockClear();
  localStorage.clear();
}

const lastBody = () => JSON.parse(fetchMock.mock.calls.at(-1)![1]!.body as string);
const lastUrl = () => String(fetchMock.mock.calls.at(-1)![0]).replace(/^https?:\/\/[^/]+/, "");
const varOf = (el: HTMLElement, name: string) => el.style.getPropertyValue(name);

describe("NowPlaying", () => {
  beforeEach(() => boot());

  it("renders metadata as an h2, badge, accent vars, labelled target indicator; passes axe", async () => {
    const { container } = render(<NowPlaying />);
    expect(screen.getByTestId("np-title").tagName).toBe("H2");
    expect(screen.getByTestId("np-title")).toHaveTextContent("Blue in Green");
    expect(screen.queryByRole("heading", { level: 1 })).toBeNull();
    expect(screen.getByText("Miles Davis · Kind of Blue")).toBeInTheDocument();
    expect(screen.getByText("Tidal")).toBeInTheDocument();
    const indicator = screen.getByTestId("target-indicator");
    expect(indicator).toHaveTextContent("Living Room Amp");
    expect(indicator).toHaveAccessibleName(/Playing on Living Room Amp\. 1 room playing/);
    const root = screen.getByTestId("now-playing");
    expect(varOf(root, "--art-accent")).toBe("#3a5a7a");
    // dark blue accent fails 3:1 against the track → text-primary fill
    expect(varOf(root, "--scrub-fill")).toBe("var(--text-primary)");
    expect(screen.getByTestId("hero-art").querySelector("img")?.getAttribute("src")).toContain("size=1080");
    expect(await axe(container)).toHaveNoViolations();
  });

  it("uses the accent as fill when it clears contrast", () => {
    const s = sampleState();
    s.now_playing["heos:heos-1"]!.art.accent = "#f0a63c";
    boot(s);
    render(<NowPlaying />);
    expect(varOf(screen.getByTestId("now-playing"), "--scrub-fill")).toBe("#f0a63c");
  });

  it("HEOS side: scrubber read-only (no thumb) and ±15 disabled, next/prev enabled", () => {
    render(<NowPlaying />);
    expect(screen.getByTestId("scrubber")).toHaveAttribute("data-seekable", "false");
    expect(screen.queryByTestId("scrub-thumb")).toBeNull();
    expect(screen.getByRole("slider", { name: "Playback position" })).toHaveAttribute("aria-readonly", "true");
    expect(screen.getByLabelText("Forward 15 seconds")).toBeDisabled();
    expect(screen.getByLabelText("Next track")).toBeEnabled();
  });

  it("falls back to bg-raised when the accent is unsafe; radio disables prev and shows --:--", () => {
    useHub.getState().selectSide("sonos:sonos-gK");
    render(<NowPlaying />);
    expect(varOf(screen.getByTestId("now-playing"), "--art-accent")).toBe("var(--bg-raised)");
    expect(screen.getByLabelText("Previous track")).toBeDisabled();
    expect(screen.getByLabelText("Next track")).toBeEnabled();
    expect(screen.getByTestId("total")).toHaveTextContent("--:--");
    expect(screen.getAllByText("Pandora").length).toBeGreaterThan(0);
  });

  it("transport buttons dispatch to the active side", async () => {
    render(<NowPlaying />);
    await userEvent.click(screen.getByRole("button", { name: "Pause" }));
    expect(lastUrl()).toBe("/api/transport/toggle");
    expect(lastBody()).toEqual({ target: "heos:heos-1" });
    await userEvent.click(screen.getByLabelText("Next track"));
    expect(lastUrl()).toBe("/api/transport/next");
  });

  it("side volume: keyboard step sends exactly one side-level command (flush is the commit)", () => {
    render(<NowPlaying />);
    fireEvent.keyDown(screen.getByTestId("side-volume"), { key: "ArrowRight" });
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(lastUrl()).toBe("/api/volume");
    expect(lastBody()).toEqual({ target: "heos:heos-1", level: 41 });
  });

  it("opens the volume sheet and the zone picker", async () => {
    render(<NowPlaying />);
    await userEvent.click(screen.getByTestId("open-volume"));
    expect(screen.getByTestId("volume-sheet")).toBeInTheDocument();
    await userEvent.click(screen.getByTestId("target-indicator"));
    expect(useChrome.getState().zonesOpen).toBe(true); // the single picker lives in PlayerChrome (A11)
  });

  it("shows skeletons (not display-size copy) and a disabled play button before the first snapshot", () => {
    useHub.getState()._reset();
    const { container } = render(<NowPlaying />);
    expect(screen.queryByTestId("np-title")).toBeNull();
    expect(container.querySelectorAll("[data-skeleton]").length).toBeGreaterThan(0);
    expect(screen.getByTestId("play-toggle")).toBeDisabled();
  });
});

describe("MiniPlayer", () => {
  beforeEach(() => boot());
  it("shows thumb, title, zone dots, sync chip slot; toggles playback; tap expands; hideArt swaps the shared element out", async () => {
    const onExpand = vi.fn();
    const { rerender } = render(<MiniPlayer onExpand={onExpand} />);
    expect(screen.getByText("Blue in Green")).toBeInTheDocument();
    expect(screen.getByTestId("mini-player").querySelector("img")?.getAttribute("src")).toContain("size=96");
    expect(screen.getByRole("img", { name: "1 room playing" })).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Pause" }));
    expect(lastUrl()).toBe("/api/transport/toggle");
    await userEvent.click(screen.getByRole("button", { name: "Open Now Playing" }));
    expect(onExpand).toHaveBeenCalled();
    rerender(<MiniPlayer onExpand={onExpand} hideArt />);
    expect(screen.getByTestId("mini-player").querySelector("img")).toBeNull();
  });
  it("marquees long titles only", () => {
    const s = sampleState();
    s.now_playing["heos:heos-1"]!.title = "A very very very long title that keeps going on";
    boot(s);
    render(<MiniPlayer onExpand={() => {}} />);
    expect(screen.getByText(/A very very/).className).toContain("marquee");
  });
  it("does not re-render on a positions-only delta for another side, but does when its own now-playing changes", () => {
    const s = sampleState();
    render(<MiniPlayer onExpand={() => {}} />);
    resetMiniPlayerRenders();
    act(() => {
      useHub.getState().onMessage({ type: "delta", from_version: 10, to_version: 11, changed: { "positions.sonos:sonos-gK": { position_ms: 5000, reported_at: new Date().toISOString(), confidence: 0.9 } } });
    });
    act(() => {
      useHub.getState().onMessage({ type: "delta", from_version: 11, to_version: 12, changed: { "positions.heos:heos-1": { position_ms: 61_000, reported_at: new Date().toISOString(), confidence: 0.9 } } });
    });
    expect(miniPlayerRenders).toBe(0);
    act(() => {
      useHub.getState().onMessage({ type: "delta", from_version: 12, to_version: 13, changed: { "now_playing.heos:heos-1": { ...s.now_playing["heos:heos-1"], title: "So What" } } });
    });
    expect(miniPlayerRenders).toBeGreaterThan(0);
    expect(screen.getByText("So What")).toBeInTheDocument();
  });
});

describe("VolumeSheet", () => {
  beforeEach(() => boot());
  it("lists master + every room interleaved, marks offline rows, mutes, and passes axe", async () => {
    const { container } = render(<VolumeSheet open onClose={() => {}} />);
    expect(screen.getByTestId("master-slider")).toHaveAttribute("aria-valuenow", "40");
    ["Kitchen", "Living Room Amp", "Patio"].forEach((r) => expect(screen.getByRole("slider", { name: `${r} volume` })).toBeInTheDocument());
    const patio = screen.getByTestId("volume-row-sonos-P");
    expect(within(patio).getByText("offline")).toBeInTheDocument();
    expect(within(patio).getByRole("slider")).toHaveAttribute("aria-disabled", "true");
    await userEvent.click(screen.getByRole("button", { name: "Mute Kitchen" }));
    expect(lastUrl()).toBe("/api/mute");
    expect(lastBody()).toEqual({ target: "sonos-K", muted: true });
    expect(await axe(container)).toHaveNoViolations();
  });
  it("master sends one linked command per step; room slider one per-player command (no double write)", () => {
    render(<VolumeSheet open onClose={() => {}} />);
    fireEvent.keyDown(screen.getByTestId("master-slider"), { key: "ArrowRight" });
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(lastBody()).toEqual({ linked: true, level: 41 });
    // the linked move patched Kitchen 20 → 21 optimistically (ratio kept), so one step down is 20
    fireEvent.keyDown(screen.getByTestId("slider-sonos-K"), { key: "ArrowLeft" });
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(lastBody()).toEqual({ target: "sonos-K", level: 20 });
  });
  it("closes on Escape and locks body scroll while open", () => {
    const onClose = vi.fn();
    const { unmount } = render(<VolumeSheet open onClose={onClose} />);
    expect(document.body.classList.contains("scroll-lock")).toBe(true);
    fireEvent.keyDown(window, { key: "Escape" });
    expect(onClose).toHaveBeenCalled();
    unmount();
    expect(document.body.classList.contains("scroll-lock")).toBe(false);
  });
});

describe("ZonePicker", () => {
  beforeEach(() => boot());
  it("sentence-case status copy", () => {
    const heos = side("h", "H", "heos", ["p"]);
    const zoneOn = { id: "z", key: "main", name: "Main zone", power: true, online: true, host: null, device_id: null, player_ids: ["p"] };
    expect(sideStatus({ ...heos, play_state: "play" }, true, [])).toEqual({ text: "Playing", tone: "signal" });
    expect(sideStatus({ ...heos, play_state: "pause" }, true, [])).toEqual({ text: "Paused", tone: "secondary" });
    expect(sideStatus(heos, true, [zoneOn])).toEqual({ text: "On", tone: "secondary" });
    expect(sideStatus(heos, true, [{ ...zoneOn, power: false }])).toEqual({ text: "Off", tone: "secondary" });
    expect(sideStatus(heos, true, [])).toEqual({ text: "Idle", tone: "secondary" });
    expect(sideStatus(side("s", "S", "sonos", ["q"]), true, [])).toEqual({ text: "Idle", tone: "secondary" });
    expect(sideStatus(heos, false, [zoneOn])).toEqual({ text: "Offline", tone: "tertiary" });
  });
  it("rows with glyph semantics, labelled power toggle on HEOS, single selection closes and becomes the active side", async () => {
    const onClose = vi.fn();
    render(<ZonePicker open onClose={onClose} />);
    const heosRow = screen.getByTestId("zone-row-heos:heos-1");
    const sonosRow = screen.getByTestId("zone-row-sonos:sonos-gK");
    expect(within(heosRow).getByText("Playing")).toBeInTheDocument();
    expect(within(sonosRow).getByText(/Idle/)).toBeInTheDocument();
    expect(within(sonosRow).getByText(/2 speakers/)).toBeInTheDocument();
    expect(within(sonosRow).queryByTestId(/power-/)).toBeNull();
    const power = screen.getByTestId("power-denon-10.0.0.9:main");
    expect(power).toHaveAccessibleName("Main zone power");
    expect(power).toHaveAttribute("aria-pressed", "true");
    expect(within(power).getByText("Main")).toBeInTheDocument();
    await userEvent.click(power);
    expect(lastUrl()).toBe("/api/zone/power");
    expect(lastBody()).toEqual({ zone_id: "denon-10.0.0.9:main", on: false });
    await userEvent.click(within(sonosRow).getByRole("button", { name: /Kitchen \+ 1/ }));
    expect(useHub.getState().selectedTargets).toEqual(["sonos:sonos-gK"]);
    expect(useHub.getState().activeSideId).toBe("sonos:sonos-gK");
    expect(onClose).toHaveBeenCalledTimes(1);
    expect(readLastTarget()).toEqual(["sonos:sonos-gK"]);
  });
  it("deselecting does not change the active side or close; multi-select stays open", async () => {
    const onClose = vi.fn();
    useHub.getState().setTargets(["heos:heos-1"]);
    useHub.getState().selectSide("heos:heos-1");
    render(<ZonePicker open onClose={onClose} />);
    await userEvent.click(within(screen.getByTestId("zone-row-sonos:sonos-gK")).getByRole("button", { name: /Kitchen/ }));
    expect(useHub.getState().selectedTargets).toEqual(["heos:heos-1", "sonos:sonos-gK"]);
    expect(onClose).not.toHaveBeenCalled();
    expect(useHub.getState().activeSideId).toBe("sonos:sonos-gK"); // selecting makes it active
    await userEvent.click(within(screen.getByTestId("zone-row-sonos:sonos-gK")).getByRole("button", { name: /Kitchen/ }));
    expect(useHub.getState().selectedTargets).toEqual(["heos:heos-1"]);
    expect(useHub.getState().activeSideId).toBe("sonos:sonos-gK"); // deselecting leaves the active side alone
    expect(onClose).not.toHaveBeenCalled();
  });
  it("pre-highlights the remembered target when opened with nothing selected", () => {
    rememberTargets(["heos:heos-1", "gone"]);
    render(<ZonePicker open onClose={() => {}} />);
    expect(useHub.getState().selectedTargets).toEqual(["heos:heos-1"]);
    expect(within(screen.getByTestId("zone-row-heos:heos-1")).getByRole("button", { name: /Living Room/ })).toHaveAttribute("aria-pressed", "true");
  });
});

describe("PlayerChrome", () => {
  beforeEach(() => {
    boot();
    useChrome.setState({ npExpanded: false, zonesOpen: false, playRequest: null });
    useLibrary.getState()._reset();
    useLibrary.setState({ _deps: { fetcher, now: () => Date.now(), timeoutMs: 8000 } });
  });

  it("starts collapsed on the route content, expands from the mini-player, collapses; body locked while expanded", async () => {
    render(
      <PlayerChrome>
        <p>route content</p>
      </PlayerChrome>,
    );
    expect(screen.getByText("route content")).toBeInTheDocument();
    expect(screen.queryByTestId("now-playing-layer")).toBeNull();
    expect(document.body.classList.contains("scroll-lock")).toBe(false);
    await userEvent.click(screen.getByRole("button", { name: "Open Now Playing" }));
    expect(screen.getByTestId("now-playing-layer")).toBeInTheDocument();
    expect(document.body.classList.contains("scroll-lock")).toBe(true);
    // expanding pins the resolved side so pausing it does not hop Now Playing elsewhere
    expect(useHub.getState().activeSideId).toBe("heos:heos-1");
    await userEvent.click(screen.getByRole("button", { name: "Collapse" }));
    await waitFor(() => expect(screen.queryByTestId("now-playing-layer")).toBeNull());
    expect(document.body.classList.contains("scroll-lock")).toBe(false);
  });

  it("a play request opens the picker in play mode with the preferred side pre-highlighted; confirming posts /api/play per side and updates the mini-player optimistically", async () => {
    render(
      <PlayerChrome>
        <span />
      </PlayerChrome>,
    );
    act(() =>
      useChrome.getState().requestPlay({
        content_ref: { service: "tidal", kind: "album", id: "77" },
        title: "Kind of Blue",
        subtitle: "Miles Davis",
        art: { url: "/api/art/k", accent: null, accent_is_safe: false },
        preferred: ["sonos:sonos-gK"],
      }),
    );
    const picker = screen.getByTestId("zone-picker");
    expect(within(picker).getByRole("heading")).toHaveTextContent("Play “Kind of Blue” on");
    const confirm = screen.getByTestId("confirm-play");
    expect(confirm).toHaveTextContent("Play on Kitchen + 1");
    await userEvent.click(confirm);
    const playCall = fetchMock.mock.calls.find((c) => String(c[0]).endsWith("/api/play"));
    expect(playCall).toBeDefined();
    expect(JSON.parse(playCall![1]!.body as string)).toEqual({ target: "sonos:sonos-gK", content_ref: { service: "tidal", kind: "album", id: "77" } });
    // optimistic: the side is playing and the mini-player shows the item
    expect(useHub.getState().state?.sides["sonos:sonos-gK"]?.play_state).toBe("play");
    expect(useHub.getState().state?.now_playing["sonos:sonos-gK"]?.title).toBe("Kind of Blue");
    expect(useHub.getState().activeSideId).toBe("sonos:sonos-gK");
    await waitFor(() => expect(screen.queryByTestId("zone-picker")).toBeNull());
    expect(useChrome.getState().playRequest).toBeNull();
    // home recents are refreshed after a play
    await waitFor(() => expect(fetchMock.mock.calls.some((c) => String(c[0]).includes("/api/home"))).toBe(true));
  });

  it("play mode: an unavailable side is disabled with a reason; toggling rows updates the button label; two rooms read 'and'", async () => {
    render(
      <PlayerChrome>
        <span />
      </PlayerChrome>,
    );
    act(() =>
      useChrome.getState().requestPlay({
        content_ref: { service: "ytmusic", kind: "playlist", id: "p1" },
        title: "Focus",
        subtitle: null,
        art: { url: null, accent: null, accent_is_safe: false },
        preferred: [],
        availability: { heos: false, sonos: true },
      }),
    );
    const heosRow = within(screen.getByTestId("zone-row-heos:heos-1")).getAllByRole("button")[0]!;
    expect(heosRow).toBeDisabled();
    expect(heosRow).toHaveTextContent("Not available on HEOS");
    const confirm = screen.getByTestId("confirm-play");
    expect(confirm).toBeDisabled();
    expect(confirm).toHaveTextContent("Choose a room");
    await userEvent.click(within(screen.getByTestId("zone-row-sonos:sonos-gK")).getAllByRole("button")[0]!);
    expect(confirm).toHaveTextContent("Play on Kitchen + 1");
    // picker stays open in play mode after a single selection
    expect(screen.getByTestId("zone-picker")).toBeInTheDocument();
  });

  it("swipe-down rule: collapse past 80px or 600px/s, only from scrollTop 0", () => {
    const pt = { x: 0, y: 0 };
    expect(shouldCollapse({ offset: { x: 0, y: 90 }, velocity: pt }, 0)).toBe(true);
    expect(shouldCollapse({ offset: { x: 0, y: 20 }, velocity: { x: 0, y: 700 } }, 0)).toBe(true);
    expect(shouldCollapse({ offset: { x: 0, y: 20 }, velocity: pt }, 0)).toBe(false);
    expect(shouldCollapse({ offset: { x: 0, y: 200 }, velocity: pt }, 10)).toBe(false);
  });
});
