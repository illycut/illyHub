/**
 * Search (Phase 8, ai-dev #78): the header field debounces at 300 ms, sends nothing under two
 * characters, groups results with badges and availability captions, says which service did not
 * answer, and shows "Nothing matched."; the home sections give way while a query is active.
 */
import { useState } from "react";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { axe } from "vitest-axe";
import { HomeScreen } from "./HomeScreen";
import { SearchBar, SearchResultsView, type SearchState } from "./Search";
import { useHub } from "@/lib/hub/store";
import { useLibrary } from "@/lib/library/store";
import { useChrome } from "@/lib/ui/chrome";
import { jsonResponse, sampleState } from "@/test/fixtures";
import type { LibraryItem, TrackItem } from "@/lib/hub/library";

const push = vi.fn();
vi.mock("next/navigation", () => ({ useRouter: () => ({ push, back: vi.fn() }), useSearchParams: () => new URLSearchParams() }));

const art = { url: "/api/art/aaaaaaaaaaaaaaaaaaaaaaaa", accent: null, accent_is_safe: false };
const album: LibraryItem = { content_ref: { service: "tidal", kind: "album", id: "a1" }, title: "Harmonic Motion", subtitle: "Signal Bloom", art, duration_ms: null, track_count: 9, availability: { heos: true, sonos: true } };
const playlist: LibraryItem = { content_ref: { service: "ytmusic", kind: "playlist", id: "p1" }, title: "Harmonic Dinner", subtitle: "12 tracks", art, duration_ms: null, track_count: 12, availability: { heos: false, sonos: true, reasons: { heos: "unsupported", sonos: null } } };
const station: LibraryItem = { content_ref: { service: "pandora", kind: "station", id: "chill-radio" }, title: "Harmonic Chill", subtitle: "Pandora station", art, duration_ms: null, track_count: null, availability: { heos: false, sonos: true, reasons: { heos: "not_linked", sonos: null } } };
const track: TrackItem = { content_ref: { service: "tidal", kind: "track", id: "t7" }, title: "Harmonic 4", subtitle: null, artist: "Signal Bloom", album: "Harmonic Motion", art, duration_ms: 201_000, track_count: null, availability: { heos: true, sonos: true }, index: 0 };
const homeBody = { recents: [], playlists: { items: [album], needs_link: [] }, favorite_albums: { items: [], needs_link: [] }, stations: { items: [], needs_link: [] } };

function boot(searchBody: (q: string) => unknown, status = 200) {
  useHub.getState()._reset();
  useHub.getState().onMessage({ type: "snapshot", version: 10, state: sampleState() });
  useChrome.setState({ npExpanded: false, zonesOpen: false, playRequest: null });
  useLibrary.getState()._reset();
  const searches: string[] = [];
  const fetcher = vi.fn(async (u: RequestInfo | URL) => {
    const url = String(u).replace(/^https?:\/\/[^/]+/, "");
    if (url.startsWith("/api/search")) {
      const q = new URLSearchParams(url.split("?")[1]).get("q") ?? "";
      searches.push(q);
      return jsonResponse(searchBody(q), status);
    }
    if (url === "/api/home") return jsonResponse(homeBody);
    return jsonResponse({});
  }) as unknown as typeof fetch;
  useLibrary.setState({ _deps: { fetcher, now: () => Date.now(), timeoutMs: 8000 } });
  push.mockClear();
  return searches;
}

describe("SearchBar", () => {
  beforeEach(() => vi.useFakeTimers({ shouldAdvanceTime: true }));
  afterEach(() => vi.useRealTimers());

  it("expands from the magnifier with autofocus, debounces 300 ms, ignores queries under two characters, and collapses + clears on Cancel and Escape", async () => {
    const searches = boot(() => ({ albums: [album], playlists: [], tracks: [], errors: {} }));
    const states: SearchState[] = [];
    const Harness = () => {
      const [open, setOpen] = useState(false);
      return <SearchBar open={open} onOpenChange={setOpen} onState={(s) => states.push(s)} />;
    };
    render(<Harness />);
    fireEvent.click(screen.getByTestId("open-search"));
    const input = screen.getByTestId("search-input") as HTMLInputElement;
    expect(document.activeElement).toBe(input);
    expect(input.closest("label")?.className).toContain("h-target");
    fireEvent.change(input, { target: { value: "h" } });
    act(() => void vi.advanceTimersByTime(400));
    expect(searches).toEqual([]);
    expect(states.at(-1)).toEqual({ phase: "idle" });
    // rapid typing: one request for the settled query
    fireEvent.change(input, { target: { value: "ha" } });
    act(() => void vi.advanceTimersByTime(100));
    fireEvent.change(input, { target: { value: "harm" } });
    act(() => void vi.advanceTimersByTime(299));
    expect(searches).toEqual([]);
    act(() => void vi.advanceTimersByTime(2));
    await waitFor(() => expect(states.some((s) => s.phase === "results")).toBe(true));
    expect(searches).toEqual(["harm"]);
    expect(states.some((s) => s.phase === "loading" && s.q === "harm")).toBe(true);
    const cancel = screen.getByTestId("cancel-search");
    expect(cancel).toHaveTextContent("Cancel");
    fireEvent.click(cancel);
    expect(screen.queryByTestId("search-input")).toBeNull();
    expect(states.at(-1)).toEqual({ phase: "idle" });
    // focus returns to the magnifier (U6)
    expect(document.activeElement).toBe(screen.getByTestId("open-search"));
    fireEvent.click(screen.getByTestId("open-search"));
    // one character: the hub's too-short sentence as the field caption, nothing sent
    fireEvent.change(screen.getByTestId("search-input"), { target: { value: "h" } });
    expect(screen.getByTestId("search-too-short")).toHaveTextContent(/at least two characters/);
    expect(screen.getByTestId("search-input")).toHaveAttribute("aria-describedby", "search-too-short");
    fireEvent.keyDown(screen.getByTestId("search-input"), { key: "Escape" });
    expect(screen.queryByTestId("search-input")).toBeNull();
    expect(document.activeElement).toBe(screen.getByTestId("open-search"));
  });
});

describe("SearchResultsView", () => {
  it("groups Albums / Playlists / Tracks with badges and availability captions; taps follow the picker rule; passes axe", async () => {
    const onPlay = vi.fn();
    const onPlayStation = vi.fn();
    const onPlayTrack = vi.fn();
    const onDetail = vi.fn();
    const state: SearchState = { phase: "results", q: "harm", results: { albums: [album], playlists: [playlist], tracks: [track], stations: [station], services: ["tidal", "ytmusic", "pandora"], errors: {}, partial: false, query: "harm" } };
    const { container } = render(<SearchResultsView state={state} onPlay={onPlay} onPlayStation={onPlayStation} onPlayTrack={onPlayTrack} onDetail={onDetail} />);
    expect(screen.getByRole("heading", { name: "Albums" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Playlists" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Tracks" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Stations" })).toBeInTheDocument();
    // one polite status line, no live region on the container (U7)
    expect(screen.getByRole("status")).toHaveTextContent("4 results for harm");
    expect(screen.getByTestId("search-results")).not.toHaveAttribute("aria-live");
    // a station routes through the Pandora rules with its availability caption and no detail chevron
    const stGrid = screen.getByTestId("search-grid-stations");
    expect(within(stGrid).getByText("Sonos rooms only")).toBeInTheDocument();
    expect(within(stGrid).queryByTestId("card-detail")).toBeNull();
    fireEvent.click(within(stGrid).getByRole("button", { name: /^Play Harmonic Chill/ }));
    expect(onPlayStation).toHaveBeenCalledWith(station);
    expect(onPlay).not.toHaveBeenCalledWith(station);
    // one-sided playlist carries the availability caption in place of its subtitle
    const plGrid = screen.getByTestId("search-grid-playlists");
    expect(within(plGrid).getByText("Sonos rooms only")).toBeInTheDocument();
    expect(within(plGrid).getByRole("img", { name: "YouTube Music" })).toBeInTheDocument();
    fireEvent.click(within(screen.getByTestId("search-grid-albums")).getByRole("button", { name: /^Play Harmonic Motion/ }));
    expect(onPlay).toHaveBeenCalledWith(album);
    fireEvent.click(within(screen.getByTestId("search-grid-albums")).getByRole("button", { name: "Open Harmonic Motion" }));
    expect(onDetail).toHaveBeenCalledWith(album);
    const row = screen.getByTestId("search-track");
    expect(row).toHaveTextContent("Signal Bloom · Harmonic Motion");
    expect(row).toHaveTextContent("3:21");
    fireEvent.click(row);
    expect(onPlayTrack).toHaveBeenCalledWith(track);
    expect(screen.queryByTestId("search-caption")).toBeNull();
    expect(await axe(container)).toHaveNoViolations();
  });

  it("per-service failure is one caption line; nothing at all reads 'Nothing matched.'; loading and error states", () => {
    // who "answered" is whoever has items in the results: a YouTube Music playlist answered, Tidal did not
    const partial: SearchState = { phase: "results", q: "x", results: { albums: [], playlists: [playlist], tracks: [], stations: [], services: ["tidal", "ytmusic", "pandora"], errors: { tidal: "timed out" }, partial: true, query: "x" } };
    const view = (state: SearchState) => <SearchResultsView state={state} onPlay={() => {}} onPlayStation={() => {}} onPlayTrack={() => {}} onDetail={() => {}} />;
    const { rerender } = render(view(partial));
    expect(screen.getByTestId("search-caption")).toHaveTextContent("Tidal didn't answer; showing YouTube Music results.");
    // every service failed: one sentence, the empty line, and the status says no results
    rerender(view({ phase: "results", q: "zz", results: { albums: [], playlists: [], tracks: [], stations: [], services: ["tidal", "ytmusic"], errors: { tidal: "timed out", ytmusic: "timed out" }, partial: true, query: "zz" } }));
    expect(screen.getByTestId("search-caption")).toHaveTextContent("Tidal and YouTube Music didn't answer.");
    expect(screen.getByTestId("search-empty")).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent("No results for zz");
    rerender(view({ phase: "results", q: "zzz", results: { albums: [], playlists: [], tracks: [], stations: [], services: ["tidal", "ytmusic", "pandora"], errors: {}, partial: false, query: "zzz" } }));
    expect(screen.getByTestId("search-empty")).toHaveTextContent("Nothing matched.");
    rerender(view({ phase: "loading", q: "zz" }));
    expect(screen.getByTestId("search-loading")).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent("Searching for zz…");
    rerender(view({ phase: "error", q: "zz", message: "The hub took too long to answer." }));
    expect(screen.getByRole("alert")).toHaveTextContent("The hub took too long to answer.");
    rerender(view({ phase: "idle" }));
    expect(screen.queryByTestId("search-results")).toBeNull();
  });
});

describe("HomeScreen with search", () => {
  beforeEach(() => vi.useFakeTimers({ shouldAdvanceTime: true }));
  afterEach(() => vi.useRealTimers());

  it("opening the field hides the title and header buttons; a query swaps the sections for results; a result tap opens the picker; Cancel restores home", async () => {
    boot(() => ({ albums: [album], playlists: [], tracks: [track], stations: [station], services: ["tidal", "pandora"], errors: {}, partial: false, query: "harmonic" }));
    const scrollTo = vi.spyOn(window, "scrollTo").mockImplementation(() => {});
    Object.defineProperty(window, "scrollY", { value: 420, configurable: true, writable: true });
    render(<HomeScreen />);
    await waitFor(() => expect(screen.getByTestId("playlists-grid")).toBeInTheDocument());
    expect(screen.getByRole("heading", { level: 1, name: "illyHub" })).toBeInTheDocument();
    fireEvent.click(screen.getByTestId("open-search"));
    // the h1 stays in the outline, visually hidden (U6)
    expect(screen.getByRole("heading", { level: 1 })).toHaveClass("sr-only");
    expect(screen.queryByTestId("open-settings")).toBeNull();
    // the sections stay in place until a query is active
    expect(screen.getByTestId("playlists-grid")).toBeInTheDocument();
    fireEvent.change(screen.getByTestId("search-input"), { target: { value: "harmonic" } });
    act(() => void vi.advanceTimersByTime(310));
    await waitFor(() => expect(screen.getByTestId("search-results")).toBeInTheDocument());
    // one <main>: the sections are gone, not hidden; the page scrolled to the top and remembered 420
    expect(document.querySelectorAll("main")).toHaveLength(1);
    expect(screen.queryByTestId("playlists-grid")).toBeNull();
    expect(scrollTo).toHaveBeenCalledWith(0, 0);
    // a station result goes through the Pandora rules (per-vendor reasons in the picker)
    fireEvent.click(within(screen.getByTestId("search-grid-stations")).getByRole("button", { name: /^Play Harmonic Chill/ }));
    expect(useChrome.getState().playRequest?.content_ref).toEqual(station.content_ref);
    expect(useChrome.getState().playRequest?.availability).toEqual(station.availability);
    expect(useChrome.getState().playRequest?.unlinked_vendors).toBeDefined();
    fireEvent.click(screen.getByTestId("search-track"));
    expect(useChrome.getState().playRequest?.content_ref).toEqual(track.content_ref);
    expect(useChrome.getState().zonesOpen).toBe(true);
    fireEvent.click(screen.getByTestId("cancel-search"));
    expect(screen.queryByTestId("search-results")).toBeNull();
    expect(screen.getByRole("heading", { level: 1, name: "illyHub" })).toBeInTheDocument();
    expect(screen.getByTestId("playlists-grid")).toBeInTheDocument();
    // scroll restored where the user was
    await waitFor(() => expect(scrollTo).toHaveBeenLastCalledWith(0, 420));
    scrollTo.mockRestore();
  });
});
