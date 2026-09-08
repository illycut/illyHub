/**
 * Phase 5 Pandora UI: home stations section (HOME-5, link-state note, error surface, subtitles),
 * picker availability copy and single-stream note, stations never sync, Now Playing radio mode,
 * station recents (availability from history or the home section).
 */
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { axe } from "vitest-axe";
import { HomeScreen, historyToPlayRequest } from "./HomeScreen";
import { NowPlaying } from "./NowPlaying";
import { RecentsRail } from "./RecentsRail";
import { ZonePicker, unavailableReason } from "./ZonePicker";
import { useHub } from "@/lib/hub/store";
import { useLibrary } from "@/lib/library/store";
import { useChrome, type PlayRequest } from "@/lib/ui/chrome";
import { PANDORA_CONCURRENT_NOTE } from "@/lib/pandora";
import { STATIONS_CANT_SYNC } from "@/lib/sync";
import { jsonResponse, sampleState, stationNowPlaying } from "@/test/fixtures";
import type { HistoryItem, Home, LibraryItem } from "@/lib/hub/library";

const push = vi.fn();
vi.mock("next/navigation", () => ({ useRouter: () => ({ push, back: vi.fn() }), useSearchParams: () => new URLSearchParams() }));

const art = { url: "/api/art/aaaaaaaaaaaaaaaaaaaaaaaa", accent: null, accent_is_safe: false };
const station = (id: string, title: string, availability = { heos: true, sonos: true }): LibraryItem => ({
  content_ref: { service: "pandora", kind: "station", id },
  title,
  subtitle: "Pandora station",
  art,
  duration_ms: null,
  track_count: null,
  availability,
});
const stationReq = (availability = { heos: true, sonos: true }, unlinked_vendors: ("heos" | "sonos")[] = []): PlayRequest => ({
  content_ref: { service: "pandora", kind: "station", id: "chill-radio" },
  title: "Chill Radio",
  subtitle: null,
  art,
  preferred: [],
  availability,
  unlinked_vendors,
});
const homeBody = (stations: unknown) => ({ recents: [], playlists: { items: [], needs_link: [] }, favorite_albums: { items: [], needs_link: [] }, stations });

function boot(state = sampleState()) {
  // A confirm in one test remembers targets per content ref; the next test must start cold.
  try {
    localStorage.clear();
  } catch {
    // jsdom without storage
  }
  useHub.getState()._reset();
  useHub.getState().onMessage({ type: "snapshot", version: 10, state });
  useChrome.setState({ npExpanded: false, zonesOpen: false, playRequest: null });
  useLibrary.getState()._reset();
  push.mockClear();
}

function bootHome(stations: unknown) {
  boot();
  const fetcher = vi.fn(async (u: RequestInfo | URL) => (String(u).includes("/api/home") ? jsonResponse(homeBody(stations)) : jsonResponse({}))) as unknown as typeof fetch;
  useLibrary.setState({ _deps: { fetcher, now: () => Date.now(), timeoutMs: 8000 } });
}

describe("Home: Pandora stations section (HOME-5)", () => {
  it("renders alphabetical station cards with the Pandora badge, availability subtitles, no chevron; tapping opens the picker with the link state; passes axe", async () => {
    bootHome({ items: [station("z", "Zen Garden", { heos: false, sonos: true }), station("a", "Acoustic Morning")], needs_link: [], linked: { heos: true, sonos: true } });
    const { container } = render(<HomeScreen />);
    const section = await screen.findByTestId("stations-section");
    expect(within(section).getByRole("heading", { name: "Pandora stations" })).toBeInTheDocument();
    const cards = within(section).getAllByTestId("grid-card");
    expect(cards.map((c) => c.querySelector(".text-body")?.textContent)).toEqual(["Acoustic Morning", "Zen Garden"]);
    // both vendors → no subtitle; one-sided → "Sonos rooms only", also in the accessible label
    expect(cards[0]!.querySelector(".text-caption")).toBeNull();
    expect(cards[1]!.querySelector(".text-caption")).toHaveTextContent("Sonos rooms only");
    expect(within(section).getByRole("button", { name: "Play Zen Garden, Sonos rooms only. Pandora" })).toBeInTheDocument();
    expect(within(section).getAllByRole("img", { name: "Pandora" })).toHaveLength(2);
    expect(within(section).queryByTestId("card-detail")).toBeNull();
    expect(within(section).queryByTestId("stations-note")).toBeNull();
    fireEvent.click(within(section).getByRole("button", { name: /^Play Zen Garden/ }));
    await waitFor(() => expect(useChrome.getState().playRequest?.content_ref).toEqual({ service: "pandora", kind: "station", id: "z" }));
    expect(useChrome.getState().playRequest?.unlinked_vendors).toEqual([]);
    expect(useChrome.getState().zonesOpen).toBe(true);
    expect(await axe(container)).toHaveNoViolations();
  });

  it("is hidden while empty (needs_link) and shows no Connect card: Pandora is linked in the vendor apps", async () => {
    bootHome({ items: [], needs_link: ["pandora"], linked: { heos: false, sonos: false } });
    render(<HomeScreen />);
    await screen.findByTestId("home");
    await waitFor(() => expect(useLibrary.getState().home.data).not.toBeNull());
    expect(screen.queryByTestId("stations-section")).toBeNull();
    expect(screen.queryByText("Pandora stations")).toBeNull();
    expect(screen.queryByText(/Connect Pandora/)).toBeNull();
  });

  it("names the rooms and the vendor app when the hub reports a vendor as not linked", async () => {
    bootHome({ items: [station("a", "A", { heos: false, sonos: true })], needs_link: [], linked: { heos: false, sonos: true } });
    render(<HomeScreen />);
    const note = await screen.findByTestId("stations-note");
    // sampleState has one HEOS side: "Living Room Amp"
    expect(note).toHaveTextContent("Living Room Amp can't play these until Pandora is added in the HEOS app.");
    expect(note.className).toContain("text-secondary");
  });

  it("surfaces a hub-side section error in the section's place, with no note", async () => {
    bootHome({ items: [], needs_link: [], error: "Pandora stations didn't load from HEOS.", linked: { heos: false, sonos: true } });
    render(<HomeScreen />);
    const section = await screen.findByTestId("stations-section");
    expect(within(section).getByRole("alert")).toHaveTextContent("Pandora stations didn't load from HEOS.");
    expect(within(section).queryByTestId("stations-note")).toBeNull();
    expect(within(section).queryByTestId("grid-card")).toBeNull();
  });
});

describe("historyToPlayRequest for a station (UX U1)", () => {
  const recent: HistoryItem = { content_ref: { service: "pandora", kind: "station", id: "chill-radio" }, title: "Chill Radio", subtitle: "Pandora station", art, last_targets: ["sonos:sonos-gK"], last_played_at: "2026-09-07T00:00:00Z", play_count: 3, availability: null };
  const home: Home = {
    recents: { items: [recent], needs_link: [], error: null, linked: null },
    playlists: { items: [], needs_link: [], error: null, linked: null },
    favorite_albums: { items: [], needs_link: [], error: null, linked: null },
    stations: { items: [station("chill-radio", "Chill Radio", { heos: false, sonos: true })], needs_link: [], error: null, linked: { heos: false, sonos: true } },
  };

  it("uses the history item's availability when the hub sends it", () => {
    const req = historyToPlayRequest({ ...recent, availability: { heos: true, sonos: false } }, home);
    expect(req.availability).toEqual({ heos: true, sonos: false });
    expect(req.unlinked_vendors).toEqual(["heos"]);
    expect(req.preferred).toEqual(["sonos:sonos-gK"]);
  });

  it("falls back to the matching station in the home section, and to nothing when neither knows", () => {
    expect(historyToPlayRequest(recent, home).availability).toEqual({ heos: false, sonos: true });
    expect(historyToPlayRequest(recent, null).availability).toBeUndefined();
    expect(historyToPlayRequest(recent, null).unlinked_vendors).toEqual([]);
    const album: HistoryItem = { ...recent, content_ref: { service: "tidal", kind: "album", id: "a1" } };
    expect(historyToPlayRequest(album, home).unlinked_vendors).toBeUndefined();
  });
});

describe("Picker with a station", () => {
  beforeEach(() => boot());

  it("an unlinked vendor's rows get the fix, a linked vendor lacking the station gets the fact; reasons read in text-secondary; passes axe", async () => {
    const { container, rerender } = render(<ZonePicker open onClose={() => {}} play={stationReq({ heos: false, sonos: true }, ["heos"])} onConfirm={() => {}} />);
    const heosRow = screen.getByTestId("zone-row-heos:heos-1").querySelector("button")!;
    expect(heosRow).toBeDisabled();
    expect(heosRow).toHaveTextContent("Pandora is set up in the HEOS app.");
    const reasonLine = within(heosRow).getByText("Pandora is set up in the HEOS app.");
    expect(reasonLine.className).toContain("text-secondary");
    expect(reasonLine.className).not.toContain("text-tertiary");
    expect(screen.getByTestId("zone-row-sonos:sonos-gK").querySelector("button")!).not.toBeDisabled();
    expect(await axe(container)).toHaveNoViolations();
    rerender(<ZonePicker open onClose={() => {}} play={stationReq({ heos: false, sonos: true }, [])} onConfirm={() => {}} />);
    expect(screen.getByTestId("zone-row-heos:heos-1").querySelector("button")!).toHaveTextContent("Not in the HEOS Pandora account.");
    const heosSide = useHub.getState().state!.sides["heos:heos-1"]!;
    expect(unavailableReason(heosSide, stationReq({ heos: false, sonos: true }, ["heos"]))).toBe("Pandora is set up in the HEOS app.");
    expect(unavailableReason(heosSide, stationReq({ heos: false, sonos: true }))).toBe("Not in the HEOS Pandora account.");
    expect(unavailableReason(useHub.getState().state!.sides["sonos:sonos-gK"]!, stationReq({ heos: false, sonos: true }))).toBeNull();
  });

  it("stations never sync: both vendors → plain Play plus one caption, no disabled Sync Play button", () => {
    const onConfirm = vi.fn();
    render(<ZonePicker open onClose={() => {}} play={stationReq()} onConfirm={onConfirm} />);
    // sampleState: the HEOS side is pre-highlighted (active, playing); add the Sonos side
    fireEvent.click(screen.getByTestId("zone-row-sonos:sonos-gK").querySelector("button")!);
    const confirm = screen.getByTestId("confirm-play");
    expect(confirm).toHaveAttribute("data-mode", "play");
    expect(confirm).toHaveTextContent(/^Play (on|in) /);
    expect(screen.queryByTestId("sync-play-disabled")).toBeNull();
    expect(screen.queryByTestId("sync-reason")).toBeNull();
    expect(screen.getByTestId("station-sync-note")).toHaveTextContent(STATIONS_CANT_SYNC);
    fireEvent.click(confirm);
    expect(onConfirm).toHaveBeenCalledWith(expect.arrayContaining(["heos:heos-1", "sonos:sonos-gK"]), { mode: "play", syncReason: STATIONS_CANT_SYNC });
  });

  it("single-stream note: two rooms, or another room already playing Pandora; described from the confirm button; never for Tidal or an empty selection", () => {
    const { rerender } = render(<ZonePicker open onClose={() => {}} play={stationReq()} onConfirm={() => {}} />);
    expect(screen.queryByTestId("pandora-note")).toBeNull();
    expect(screen.getByTestId("confirm-play")).not.toHaveAttribute("aria-describedby");
    fireEvent.click(screen.getByTestId("zone-row-sonos:sonos-gK").querySelector("button")!);
    const note = screen.getByTestId("pandora-note");
    expect(note).toHaveTextContent(PANDORA_CONCURRENT_NOTE);
    expect(note).toHaveAttribute("id", "pandora-note");
    expect(note.className).toContain("text-micro");
    expect(screen.getByTestId("confirm-play")).toHaveAttribute("aria-describedby", "pandora-note");
    // deselect: back to one room, nobody else plays Pandora → no note
    fireEvent.click(screen.getByTestId("zone-row-sonos:sonos-gK").querySelector("button")!);
    expect(screen.queryByTestId("pandora-note")).toBeNull();
    // the Sonos group starts playing Pandora elsewhere (hub delta) while HEOS alone is selected
    act(() => useHub.getState().onMessage({ type: "delta", from_version: 10, to_version: 11, changed: { "sides.sonos:sonos-gK": { ...useHub.getState().state!.sides["sonos:sonos-gK"], play_state: "play" } } }));
    expect(screen.getByTestId("pandora-note")).toHaveTextContent(PANDORA_CONCURRENT_NOTE);
    // deselect everything: no note with an empty selection
    fireEvent.click(screen.getByTestId("zone-row-heos:heos-1").querySelector("button")!);
    expect(screen.queryByTestId("pandora-note")).toBeNull();
    // Tidal content: never
    const tidal: PlayRequest = { ...stationReq(), content_ref: { service: "tidal", kind: "album", id: "a1" }, title: "Kind of Blue" };
    rerender(<ZonePicker open onClose={() => {}} play={tidal} onConfirm={() => {}} />);
    fireEvent.click(screen.getByTestId("zone-row-sonos:sonos-gK").querySelector("button")!);
    expect(screen.queryByTestId("pandora-note")).toBeNull();
  });
});

describe("Now Playing radio mode", () => {
  it("track on the title, station on the album line, read-only slider without a thumb, track total shown, prev disabled, next enabled, Pandora badge, no Sync Play offer", () => {
    boot();
    useHub.getState().selectSide("sonos:sonos-gK");
    render(<NowPlaying />);
    expect(screen.getByTestId("np-title")).toHaveTextContent("Neon Skyline 1");
    expect(screen.getByText("Signal Bloom · Chill Radio")).toBeInTheDocument();
    expect(screen.getByRole("slider", { name: "Playback position" })).toHaveAttribute("aria-readonly", "true");
    expect(screen.queryByTestId("scrub-thumb")).toBeNull();
    expect(screen.getByTestId("total")).toHaveTextContent("3:00");
    expect(screen.getByRole("button", { name: "Previous track" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Next track" })).not.toBeDisabled();
    expect(screen.getByTestId("service-chip").parentElement).toHaveTextContent("Pandora");
    // the station ref is non-null but not Tidal: no Sync Play offer
    expect(useHub.getState().state!.now_playing["sonos:sonos-gK"]!.content_ref).toEqual({ service: "pandora", kind: "station", id: "chill-radio" });
    expect(screen.queryByTestId("offer-sync")).toBeNull();
  });

  it("with no duration: no slider and no track, the elapsed timecode only", () => {
    const state = sampleState();
    state.now_playing["sonos:sonos-gK"] = stationNowPlaying({ duration_ms: null });
    boot(state);
    useHub.getState().selectSide("sonos:sonos-gK");
    render(<NowPlaying />);
    expect(screen.queryByRole("slider", { name: "Playback position" })).toBeNull();
    expect(screen.queryByRole("progressbar")).toBeNull();
    expect(screen.queryByTestId("scrub-thumb")).toBeNull();
    expect(screen.queryByTestId("total")).toBeNull();
    expect(screen.getByRole("timer", { name: "Elapsed" })).toHaveTextContent("0:00");
    expect(screen.getByTestId("scrubber")).toHaveAttribute("data-seekable", "false");
  });
});

describe("Recents rail with a station", () => {
  it("shows the Pandora badge and the last-played glyph, the availability subtitle when known, and no detail affordance", () => {
    boot();
    const recent: HistoryItem = { content_ref: { service: "pandora", kind: "station", id: "chill-radio" }, title: "Chill Radio", subtitle: "Pandora station", art, last_targets: ["sonos:sonos-gK"], last_played_at: "2026-09-07T00:00:00Z", play_count: 3, availability: { heos: false, sonos: true } };
    const onPlay = vi.fn();
    const { rerender } = render(<RecentsRail items={[recent]} loading={false} onPlay={onPlay} onDetail={() => {}} />);
    const rail = screen.getByTestId("recents-rail");
    expect(within(rail).getByRole("img", { name: "Pandora" })).toBeInTheDocument();
    expect(within(rail).queryByTestId("card-detail")).toBeNull();
    fireEvent.click(within(rail).getByRole("button", { name: "Play Chill Radio, Sonos rooms only. Pandora. Last played on Kitchen + 1" }));
    expect(onPlay).toHaveBeenCalledWith(recent);
    // availability unknown: the hub's subtitle stands
    rerender(<RecentsRail items={[{ ...recent, availability: null }]} loading={false} onPlay={onPlay} onDetail={() => {}} />);
    expect(within(screen.getByTestId("recents-rail")).getByRole("button", { name: /Play Chill Radio, Pandora station\. Pandora/ })).toBeInTheDocument();
  });
});
