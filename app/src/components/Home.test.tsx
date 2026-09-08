import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { axe } from "vitest-axe";
import { ArtCard, artCardLabel, artCardRenders, resetArtCardRenders } from "./ArtCard";
import { RecentsRail, lastSideOf, sideInfo } from "./RecentsRail";
import { CardGrid, HomeScreen, detailHref } from "./HomeScreen";
import { useHub } from "@/lib/hub/store";
import { useLibrary } from "@/lib/library/store";
import { useChrome } from "@/lib/ui/chrome";
import { jsonResponse, sampleState } from "@/test/fixtures";
import type { HistoryItem, LibraryItem } from "@/lib/hub/library";

const push = vi.fn();
vi.mock("next/navigation", () => ({ useRouter: () => ({ push, back: vi.fn() }), useSearchParams: () => new URLSearchParams() }));

const art = { url: "/api/art/aaaaaaaaaaaaaaaaaaaaaaaa", accent: null, accent_is_safe: false };
const album: LibraryItem = { content_ref: { service: "tidal", kind: "album", id: "a1" }, title: "Kind of Blue", subtitle: "Miles Davis", art, duration_ms: null, track_count: 9, availability: { heos: true, sonos: true } };
const playlist: LibraryItem = { content_ref: { service: "ytmusic", kind: "playlist", id: "p1" }, title: "Focus", subtitle: "12 tracks", art, duration_ms: null, track_count: 12, availability: { heos: false, sonos: true } };
const recent: HistoryItem = { content_ref: album.content_ref, title: album.title, subtitle: album.subtitle, art, last_targets: ["sonos:sonos-gK"], last_played_at: "2026-09-07T00:00:00Z", play_count: 1, availability: null };
const homeBody = (over: Record<string, unknown> = {}) => ({ recents: [], playlists: { items: [], needs_link: [] }, favorite_albums: { items: [], needs_link: [] }, stations: { items: [], needs_link: [] }, ...over });

function bootHome(responder: (calls: number) => unknown) {
  useHub.getState()._reset();
  useHub.getState().onMessage({ type: "snapshot", version: 10, state: sampleState() });
  useChrome.setState({ npExpanded: false, zonesOpen: false, playRequest: null });
  useLibrary.getState()._reset();
  let n = 0;
  const fetcher = vi.fn(async (u: RequestInfo | URL) => (String(u).includes("/api/home") ? jsonResponse(responder(++n)) : jsonResponse({}))) as unknown as typeof fetch;
  useLibrary.setState({ _deps: { fetcher, now: () => Date.now(), timeoutMs: 8000 } });
  push.mockClear();
  return fetcher as unknown as ReturnType<typeof vi.fn>;
}

describe("ArtCard", () => {
  it("renders art at 320, badge chip, corner glyph, a full accessible label, and a separate detail affordance; passes axe", async () => {
    const onPress = vi.fn();
    const onDetail = vi.fn();
    const { container } = render(<ArtCard payload="p" title="Kind of Blue" subtitle="Miles Davis" art={art} service="tidal" lastVendor="heos" lastRoom="Living Room Amp" onPress={onPress} onDetail={onDetail} />);
    expect(container.querySelector("img")?.getAttribute("src")).toContain("size=320");
    expect(screen.getByRole("img", { name: "Tidal" })).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Play Kind of Blue, Miles Davis. Tidal. Last played on Living Room Amp" }));
    expect(onPress).toHaveBeenCalledWith("p");
    await userEvent.click(screen.getByRole("button", { name: "Open Kind of Blue" }));
    expect(onDetail).toHaveBeenCalledWith("p");
    expect(artCardLabel("X", null, "pandora", null)).toBe("Play X. Pandora");
    // the chevron sits inside the card box (no negative top margin) and the text row is a full target tall
    const chevron = screen.getByTestId("card-detail");
    expect(chevron.className).not.toMatch(/-mt-|-mr-/);
    expect(chevron.parentElement?.className).toContain("min-h-target");
    expect(await axe(container)).toHaveNoViolations();
  });
});

describe("RecentsRail", () => {
  it("shows the §8 empty copy, skeletons while loading, cards with the last-played room, and bleeds by the screen margin token", () => {
    useHub.getState()._reset();
    useHub.getState().onMessage({ type: "snapshot", version: 10, state: sampleState() });
    const sides = { "sonos:sonos-gK": sideInfo("sonos", "Kitchen + 1") };
    const { rerender } = render(<RecentsRail items={[]} loading={false} onPlay={() => {}} onDetail={() => {}} />);
    expect(screen.getByText("Play something and it lands here.")).toBeInTheDocument();
    rerender(<RecentsRail items={[]} loading onPlay={() => {}} onDetail={() => {}} />);
    expect(document.querySelectorAll("[data-skeleton]").length).toBeGreaterThan(0);
    rerender(<RecentsRail items={[recent]} loading={false} onPlay={() => {}} onDetail={() => {}} />);
    const rail = screen.getByTestId("recents-rail");
    expect(rail.className).toContain("-mx-[var(--screen-margin)]");
    expect(within(rail).getByRole("button", { name: /Last played on Kitchen \+ 1/ })).toBeInTheDocument();
    expect(lastSideOf(recent, sides)).toEqual({ vendor: "sonos", room: "Kitchen + 1" });
    expect(lastSideOf({ ...recent, last_targets: ["heos:gone"] }, sides)).toEqual({ vendor: "heos", room: null });
    expect(lastSideOf({ ...recent, last_targets: [] }, sides)).toEqual({ vendor: null, room: null });
  });

  it("does not re-render its cards on a positions-only hub delta (A5)", async () => {
    useHub.getState()._reset();
    useHub.getState().onMessage({ type: "snapshot", version: 10, state: sampleState() });
    render(<RecentsRail items={[recent]} loading={false} onPlay={() => {}} onDetail={() => {}} />);
    resetArtCardRenders();
    act(() => useHub.getState().onMessage({ type: "delta", from_version: 10, to_version: 11, changed: { "positions.heos:heos-1": { position_ms: 61_000, reported_at: new Date().toISOString(), confidence: 0.9 } } }));
    act(() => useHub.getState().onMessage({ type: "delta", from_version: 11, to_version: 12, changed: { "positions.heos:heos-1": { position_ms: 62_000, reported_at: new Date().toISOString(), confidence: 0.9 } } }));
    expect(artCardRenders).toBe(0);
    // a side rename does re-render (the label changes)
    const s = useHub.getState().state!;
    act(() => useHub.getState().onMessage({ type: "delta", from_version: 12, to_version: 13, changed: { "sides.sonos:sonos-gK": { ...s.sides["sonos:sonos-gK"], name: "Kitchen" } } }));
    await waitFor(() => expect(artCardRenders).toBe(1));
  });
});

describe("CardGrid", () => {
  it("renders connect cards for both hub-linked services when the section needs a link; each routes to its own flow (Phase 6)", async () => {
    const onConnect = vi.fn();
    render(<CardGrid section={{ items: [], needs_link: ["tidal"], error: null, linked: null }} loading={false} connect={["tidal", "ytmusic"]} onPlay={() => {}} onDetail={() => {}} onConnect={onConnect} emptyCopy="x" testId="g" />);
    const cards = screen.getAllByTestId("connect-card");
    expect(cards.map((c) => c.textContent)).toEqual(["Connect Tidal", "Connect YouTube Music"]);
    await userEvent.click(cards[0]!);
    expect(onConnect).toHaveBeenCalledWith("tidal");
    await userEvent.click(cards[1]!);
    expect(onConnect).toHaveBeenCalledWith("ytmusic");
    expect(cards[1]).not.toHaveAttribute("aria-disabled");
    expect(cards[1]!.tagName).toBe("BUTTON");
  });
  it("shows skeletons before data, empty copy with no items, and a section error as an alert", () => {
    const { rerender } = render(<CardGrid section={null} loading connect={[]} onPlay={() => {}} onDetail={() => {}} onConnect={() => {}} emptyCopy="Nothing here." testId="g" />);
    expect(document.querySelectorAll("[data-skeleton]").length).toBe(4);
    rerender(<CardGrid section={{ items: [], needs_link: [], error: null, linked: null }} loading={false} connect={[]} onPlay={() => {}} onDetail={() => {}} onConnect={() => {}} emptyCopy="Nothing here." testId="g" />);
    expect(screen.getByText("Nothing here.")).toBeInTheDocument();
    rerender(<CardGrid section={{ items: [], needs_link: [], error: "Tidal timed out.", linked: null }} loading={false} connect={[]} onPlay={() => {}} onDetail={() => {}} onConnect={() => {}} emptyCopy="Nothing here." testId="g" />);
    expect(screen.getByRole("alert")).toHaveTextContent("Tidal timed out.");
  });
});

describe("HomeScreen", () => {
  it("loads /api/home, renders rail + grids, stations hidden while empty; tap opens the picker in play mode with history targets; chevron routes to detail; passes axe", async () => {
    const fetcher = bootHome(() => homeBody({ recents: [recent], playlists: { items: [playlist], needs_link: [] }, favorite_albums: { items: [album], needs_link: [] } }));
    const { container } = render(<HomeScreen />);
    await waitFor(() => expect(screen.getByTestId("recents-rail")).toBeInTheDocument());
    expect(fetcher.mock.calls.some((c) => String(c[0]).includes("/api/home"))).toBe(true);
    expect(screen.getByTestId("playlists-grid")).toHaveTextContent("Focus");
    expect(screen.getByTestId("albums-grid")).toHaveTextContent("Kind of Blue");
    expect(screen.queryByText("Pandora stations")).toBeNull();

    await userEvent.click(within(screen.getByTestId("recents-rail")).getByRole("button", { name: /^Play Kind of Blue/ }));
    expect(useChrome.getState().playRequest?.preferred).toEqual(["sonos:sonos-gK"]);
    expect(useChrome.getState().zonesOpen).toBe(true);

    await userEvent.click(within(screen.getByTestId("playlists-grid")).getByRole("button", { name: "Open Focus" }));
    expect(push).toHaveBeenCalledWith(detailHref(playlist.content_ref));
    expect(detailHref(playlist.content_ref)).toBe("/browse?ref=ytmusic%3Aplaylist%3Ap1");
    expect(screen.getByTestId("open-settings")).toHaveAttribute("href", "/settings");
    expect(await axe(container)).toHaveNoViolations();
  });

  it("needs_link sections show connect cards that route to Settings with the service", async () => {
    // both grids report both services unlinked; the screen still shows each card once, in the first grid
    bootHome(() => homeBody({ playlists: { items: [], needs_link: ["tidal", "ytmusic"], linked: { tidal: false, ytmusic: false } }, favorite_albums: { items: [], needs_link: ["tidal", "ytmusic"], linked: { tidal: false, ytmusic: false } } }));
    render(<HomeScreen />);
    // one card per unlinked hub-linked service, once per screen (in the first grid), never a duplicate across grids (B2)
    await waitFor(() => expect(screen.getAllByTestId("connect-card").length).toBe(2));
    expect(screen.getAllByTestId("connect-card").map((c) => c.textContent)).toEqual(["Connect Tidal", "Connect YouTube Music"]);
    expect(within(screen.getByTestId("playlists-grid")).getAllByTestId("connect-card")).toHaveLength(2);
    expect(screen.getByRole("button", { name: "Connect YouTube Music" })).toBeInTheDocument();
    await userEvent.click(screen.getAllByTestId("connect-card")[0]!);
    expect(push).toHaveBeenCalledWith("/settings?link=tidal");
    await userEvent.click(screen.getAllByTestId("connect-card")[1]!);
    expect(push).toHaveBeenCalledWith("/settings?link=ytmusic");
    expect(screen.getByText("Play something and it lands here.")).toBeInTheDocument();
  });

  it("an empty rail fills after a play refreshes home (A16)", async () => {
    bootHome((n) => (n === 1 ? homeBody() : homeBody({ recents: [recent] })));
    render(<HomeScreen />);
    await waitFor(() => expect(screen.getByText("Play something and it lands here.")).toBeInTheDocument());
    act(() => useLibrary.getState().invalidateHome());
    await waitFor(() => expect(screen.getByTestId("recents-rail")).toHaveTextContent("Kind of Blue"));
  });

  it("surfaces a home load error without data as an alert", async () => {
    useHub.getState()._reset();
    useLibrary.getState()._reset();
    useLibrary.setState({ _deps: { fetcher: (async () => jsonResponse({ code: "vendor_error", message: "Tidal isn't answering." }, 502)) as unknown as typeof fetch, now: () => Date.now(), timeoutMs: 8000 } });
    render(<HomeScreen />);
    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("Tidal isn't answering."));
  });
});
