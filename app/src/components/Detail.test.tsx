import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { axe } from "vitest-axe";
import { BrowseDetail, TRACK_ROW_PX, isCurrentTrack, trackAvailabilityNote } from "./BrowseDetail";
import { VirtualList, windowRange } from "./VirtualList";
import { useHub } from "@/lib/hub/store";
import { useLibrary } from "@/lib/library/store";
import { useChrome } from "@/lib/ui/chrome";
import { jsonResponse, sampleState } from "@/test/fixtures";
import type { TrackItem } from "@/lib/hub/library";

const back = vi.fn();
vi.mock("next/navigation", () => ({ useRouter: () => ({ push: vi.fn(), back }), useSearchParams: () => new URLSearchParams() }));

const ref = { service: "tidal" as const, kind: "album" as const, id: "a1" };
const art = { url: "/api/art/aaaaaaaaaaaaaaaaaaaaaaaa", accent: null, accent_is_safe: false };
const item = { content_ref: ref, title: "Kind of Blue", subtitle: "Miles Davis", art, duration_ms: null, track_count: 3, availability: { heos: true, sonos: true }, index: null, album_id: null, artist: "Miles Davis", album: null };
/** Hub-canonical index starts at 5 so it is visibly different from the array position. */
const track = (i: number, over: Partial<TrackItem> = {}): TrackItem => ({
  content_ref: { service: "tidal", kind: "track", id: `t${i}` },
  title: `Track ${i + 1}`,
  subtitle: null,
  artist: "Miles Davis",
  album: "Kind of Blue",
  art,
  index: i + 5,
  duration_ms: 200_000 + i * 1000,
  track_count: null,
  availability: { heos: true, sonos: true },
  ...over,
});

function boot(detail: unknown, status = 200) {
  useHub.getState()._reset();
  useHub.getState().onMessage({ type: "snapshot", version: 10, state: sampleState() });
  useHub.getState().selectSide("heos:heos-1");
  useChrome.setState({ npExpanded: false, zonesOpen: false, playRequest: null });
  useLibrary.getState()._reset();
  useLibrary.setState({ _deps: { fetcher: (async () => jsonResponse(detail, status)) as unknown as typeof fetch, now: () => Date.now(), timeoutMs: 8000 } });
  localStorage.clear();
}

describe("windowRange", () => {
  it("windows rows around the scroll position with overscan and clamps at both ends", () => {
    expect(windowRange(0, 56, 0, 0, 800)).toEqual({ start: 0, end: 0 });
    expect(windowRange(500, 56, 300, 0, 800)).toEqual({ start: 0, end: 21 });
    const mid = windowRange(500, 56, 300, 5600, 800);
    expect(mid.start).toBe(94 - 6);
    expect(mid.end).toBe(94 + 15 + 6);
    expect(windowRange(100, 56, 0, 100_000, 800)).toEqual({ start: 100, end: 100 });
  });
});

describe("VirtualList", () => {
  it("renders plainly under the threshold; above it, rows are absolutely positioned direct <li> children of the <ul>, with the divider off on the last row; passes axe (A6)", async () => {
    const items = Array.from({ length: 200 }, (_, i) => i);
    const { rerender, container } = render(<VirtualList items={items.slice(0, 3)} rowHeight={56} testId="l" rowTestId="r" render={(i) => <span>{i}</span>} />);
    let list = screen.getByTestId("l");
    expect(list.querySelectorAll(":scope > li").length).toBe(3);
    expect(list.querySelectorAll(":scope > li")[2]!.className).not.toContain("border-b");
    expect(list.querySelectorAll(":scope > li")[0]!.className).toContain("border-b");
    rerender(<VirtualList items={items} rowHeight={56} testId="l" rowTestId="r" render={(i) => <span>{i}</span>} />);
    list = screen.getByTestId("l");
    expect(list).toHaveAttribute("data-virtual", "true");
    expect(list.style.height).toBe(`${200 * 56}px`);
    const lis = list.querySelectorAll(":scope > li");
    expect(lis.length).toBeGreaterThan(0);
    expect(lis.length).toBeLessThan(200);
    expect(list.children.length).toBe(lis.length); // nothing but <li> under the <ul>
    expect((lis[1] as HTMLElement).style.top).toBe("56px");
    expect(await axe(container)).toHaveNoViolations();
  });
});

describe("track helpers", () => {
  it("availability note names the side that cannot play, or the only side that can when none is chosen", () => {
    expect(trackAvailabilityNote(track(0), "heos")).toBeNull();
    expect(trackAvailabilityNote(track(0, { availability: { heos: false, sonos: true } }), "heos")).toBe("Not available on HEOS");
    expect(trackAvailabilityNote(track(0, { availability: { heos: false, sonos: true } }), null)).toBe("Sonos only");
    expect(trackAvailabilityNote(track(0, { availability: { heos: true, sonos: false } }), null)).toBe("HEOS only");
    expect(trackAvailabilityNote(track(0, { availability: { heos: false, sonos: false } }), null)).toBe("Not available");
  });
  it("current track matches by id when the hub reports one, else by title and artist (U10)", () => {
    const np = sampleState().now_playing["heos:heos-1"]!;
    expect(isCurrentTrack(track(0), { ...np, track_id: "t0" })).toBe(true);
    expect(isCurrentTrack(track(0), { ...np, track_id: "tidal:t0" })).toBe(true);
    expect(isCurrentTrack(track(1), { ...np, track_id: "t0" })).toBe(false);
    expect(isCurrentTrack(track(2), { ...np, track_id: null, title: "Track 3", artist: "Miles Davis" })).toBe(true);
    expect(isCurrentTrack(track(2), { ...np, track_id: null, title: "Track 3", artist: "Someone" })).toBe(false);
    expect(isCurrentTrack(track(2), null)).toBe(false);
  });
});

describe("BrowseDetail", () => {
  it("loads the detail, renders hero/title/meta, Play on… opens the picker for the album; a track row plays from its hub index; flagged tracks stay visible; the current track is marked; passes axe", async () => {
    const s = sampleState();
    s.now_playing["heos:heos-1"]!.track_id = "t2";
    boot({ item, tracks: [track(0), track(1, { availability: { heos: false, sonos: true } }), track(2)] });
    useHub.getState().onMessage({ type: "snapshot", version: 10, state: s });
    useHub.getState().selectSide("heos:heos-1");
    const { container } = render(<BrowseDetail contentRef={ref} />);
    await waitFor(() => expect(screen.getByTestId("detail-title")).toHaveTextContent("Kind of Blue"));
    expect(screen.getByText("Miles Davis · 3 tracks")).toBeInTheDocument();
    expect(screen.getByTestId("detail-art").getAttribute("src")).toContain("size=1080");
    const rows = screen.getAllByTestId("track-row");
    expect(rows).toHaveLength(3);
    expect(rows[0]).toHaveTextContent("6"); // hub index 5 -> displayed 6
    expect(rows[1]).toHaveTextContent("Not available on HEOS");
    expect(rows[0]).toHaveTextContent("3:20");
    expect(screen.getAllByTestId("track-li")[0]!.style.height).toBe(`${TRACK_ROW_PX}px`);
    expect(rows[2]).toHaveAttribute("aria-current", "true");
    expect(rows[0]).not.toHaveAttribute("aria-current");
    expect(within(rows[2]!).getByText("Track 3").className).toContain("text-signal");

    await userEvent.click(screen.getByTestId("detail-play"));
    expect(useChrome.getState().playRequest).toMatchObject({ content_ref: ref, title: "Kind of Blue", start_index: undefined });

    await userEvent.click(rows[2]!);
    expect(useChrome.getState().playRequest).toMatchObject({ content_ref: ref, title: "Track 3", start_index: 7 });
    expect(await axe(container)).toHaveNoViolations();
  });

  it("uses the remembered per-content targets as the pre-highlight", async () => {
    boot({ item, tracks: [] });
    localStorage.setItem("illyhub.lastTarget.v2", JSON.stringify({ order: ["tidal:album:a1"], map: { "tidal:album:a1": ["sonos:sonos-gK"] } }));
    render(<BrowseDetail contentRef={ref} />);
    await waitFor(() => expect(screen.getByTestId("detail-play")).toBeInTheDocument());
    await userEvent.click(screen.getByTestId("detail-play"));
    expect(useChrome.getState().playRequest?.preferred).toEqual(["sonos:sonos-gK"]);
    expect(screen.getByText("No tracks to show.")).toBeInTheDocument();
  });

  it("needs_link while loading renders the Connect affordance, not an error (A16)", async () => {
    boot({ code: "needs_link", message: "Tidal is not connected. Link it in Settings.", service: "tidal" }, 409);
    render(<BrowseDetail contentRef={ref} />);
    await waitFor(() => expect(screen.getByTestId("detail-needs-link")).toBeInTheDocument());
    expect(screen.getByRole("link", { name: "Connect Tidal" })).toHaveAttribute("href", "/settings?link=tidal");
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("shows other hub errors as an alert and a working back button", async () => {
    boot({ code: "vendor_error", message: "Tidal isn't answering." }, 502);
    render(<BrowseDetail contentRef={ref} />);
    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("Tidal isn't answering."));
    await userEvent.click(screen.getByTestId("back"));
    expect(back).toHaveBeenCalled();
  });
});
