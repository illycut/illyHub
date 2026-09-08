/**
 * Up next (Phase 8, ai-dev #77): the queue sheet highlights the playing entry, jumps by the hub's
 * index, shows the truncation note, says stations have no queue, and fetches when the hub has not
 * streamed a queue yet.
 */
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { axe } from "vitest-axe";
import { QueueSheet } from "./QueueSheet";
import { NowPlaying } from "./NowPlaying";
import { useHub } from "@/lib/hub/store";
import { useToasts } from "@/lib/ui/toasts";
import { jsonResponse, okAck, sampleState } from "@/test/fixtures";
import type { HubState } from "@/lib/hub/types";
import type { Queue } from "@/lib/hub/library";

vi.mock("framer-motion", async () => {
  const actual = await vi.importActual<typeof import("framer-motion")>("framer-motion");
  return { ...actual, AnimatePresence: ({ children }: { children: React.ReactNode }) => <>{children}</> };
});

const art = { url: null, accent: null, accent_is_safe: false };
const queue = (over: Partial<Queue> = {}): Queue => ({
  items: [
    { index: 0, title: "So What", artist: "Miles Davis", album: "Kind of Blue", duration_ms: 562_000, art, track_id: "t0", content_ref: { service: "tidal", kind: "track", id: "0" } },
    { index: 1, title: "Blue in Green", artist: "Miles Davis", album: "Kind of Blue", duration_ms: 337_000, art, track_id: "t1", content_ref: { service: "tidal", kind: "track", id: "1" } },
    { index: 2, title: "All Blues", artist: "Miles Davis", album: "Kind of Blue", duration_ms: 693_000, art, track_id: "t2", content_ref: { service: "tidal", kind: "track", id: "2" } },
  ],
  current_index: 1,
  source: "tidal",
  truncated: false,
  ...over,
});

const fetchMock = vi.fn();
const fetcher = fetchMock as unknown as typeof fetch;
const calls = () => fetchMock.mock.calls.map((c) => [String(c[0]).replace(/^https?:\/\/[^/]+/, ""), c[1]?.body ? JSON.parse(c[1].body as string) : undefined] as const);

function boot(state: HubState = sampleState(), responder?: (url: string, init?: RequestInit) => Response | undefined) {
  useHub.getState()._reset();
  useToasts.setState({ toasts: [] });
  fetchMock.mockReset();
  fetchMock.mockImplementation(async (u: RequestInfo | URL, init?: RequestInit) => {
    const url = String(u).replace(/^https?:\/\/[^/]+/, "");
    return responder?.(url, init) ?? jsonResponse(okAck((init?.headers as Record<string, string>)?.["x-correlation-id"]));
  });
  useHub.setState({ _deps: { fetcher, now: () => Date.now(), setTimer: (fn, ms) => setTimeout(fn, ms), clearTimer: (t) => clearTimeout(t as ReturnType<typeof setTimeout>) } });
  useHub.getState().onMessage({ type: "snapshot", version: 10, state });
  useHub.getState().setPhase("open", Date.now());
}

describe("QueueSheet", () => {
  it("lists the streamed queue with the current entry highlighted (amber title, aria-current), tap jumps by the hub's index, closes, and holds the play intent; passes axe", async () => {
    const s = sampleState();
    s.queues = { "heos:heos-1": queue() };
    boot(s);
    const onClose = vi.fn();
    const { container } = render(<QueueSheet open sideId="heos:heos-1" onClose={onClose} />);
    const rows = screen.getAllByTestId("queue-row");
    expect(rows).toHaveLength(3);
    expect(rows[1]).toHaveAttribute("data-current", "true");
    const current = within(rows[1]!).getByRole("button");
    expect(current).toHaveAttribute("aria-current", "true");
    expect(within(current).getByText("Blue in Green").className).toContain("text-signal");
    expect(within(rows[0]!).getByText("So What").className).toContain("text-primary");
    // durations are tabular
    expect(within(rows[0]!).getByText("9:22").className).toContain("numeric");
    expect(screen.queryByTestId("queue-truncated")).toBeNull();
    // no fetch: the hub already streamed the queue
    expect(calls().filter(([u]) => u.startsWith("/api/queue/"))).toHaveLength(0);
    expect(await axe(container)).toHaveNoViolations();

    fireEvent.click(screen.getByRole("button", { name: "Play from All Blues, Miles Davis" }));
    expect(onClose).toHaveBeenCalled();
    await waitFor(() => expect(calls().some(([u, b]) => u === "/api/queue/jump" && b?.target === "heos:heos-1" && b?.index === 2)).toBe(true));
    // optimistic: current moved, side playing, now-playing shows the entry, intent held
    const st = useHub.getState();
    expect(st.state?.queues?.["heos:heos-1"]?.current_index).toBe(2);
    expect(st.state?.sides["heos:heos-1"]?.play_state).toBe("play");
    expect(st.state?.now_playing["heos:heos-1"]?.title).toBe("All Blues");
    expect(st.intents["heos:heos-1"]).toBe("play");
  });

  it("truncated queues carry the note; the hub's index wins over matching", () => {
    const s = sampleState();
    s.queues = { "heos:heos-1": queue({ truncated: true, current_index: 0 }) };
    boot(s);
    render(<QueueSheet open sideId="heos:heos-1" onClose={() => {}} />);
    expect(screen.getByTestId("queue-truncated")).toHaveTextContent("Showing the first 3 tracks.");
    expect(screen.getAllByTestId("queue-row")[0]).toHaveAttribute("data-current", "true");
  });

  it("stations have no queue: the sentence, no list, no fetch", () => {
    boot();
    render(<QueueSheet open sideId="sonos:sonos-gK" onClose={() => {}} />);
    expect(screen.getByTestId("queue-station")).toHaveTextContent("Stations don't have a queue.");
    expect(screen.queryByTestId("queue-list")).toBeNull();
    expect(calls().filter(([u]) => u.startsWith("/api/queue/"))).toHaveLength(0);
  });

  it("with no streamed queue it fetches GET /api/queue/{side} once on open and renders the answer; a failed fetch shows the empty line", async () => {
    boot(sampleState(), (url) => (url === "/api/queue/heos%3Aheos-1" ? jsonResponse(queue({ current_index: null })) : undefined));
    const first = render(<QueueSheet open sideId="heos:heos-1" onClose={() => {}} />);
    expect(document.querySelectorAll("[data-skeleton]").length).toBeGreaterThan(0);
    await waitFor(() => expect(screen.getAllByTestId("queue-row")).toHaveLength(3));
    // current resolved by track id from now-playing (t1) when the hub gives no index
    expect(screen.getAllByTestId("queue-row")[1]).toHaveAttribute("data-current", "true");
    expect(calls().filter(([u]) => u === "/api/queue/heos%3Aheos-1")).toHaveLength(1);
    first.unmount();

    boot(sampleState(), (url) => (url.startsWith("/api/queue/") ? jsonResponse({ code: "vendor_error", message: "no" }, 502) : undefined));
    render(<QueueSheet open sideId="heos:heos-1" onClose={() => {}} />);
    await waitFor(() => expect(screen.getByTestId("queue-empty")).toHaveTextContent("Nothing queued."));
  });

  it("a queues.<side> delta updates the open sheet live", async () => {
    const s = sampleState();
    s.queues = { "heos:heos-1": queue() };
    boot(s);
    render(<QueueSheet open sideId="heos:heos-1" onClose={() => {}} />);
    act(() => {
      useHub.getState().onMessage({ type: "delta", from_version: 10, to_version: 11, changed: { "queues.heos:heos-1": queue({ current_index: 2 }) } });
    });
    expect(screen.getAllByTestId("queue-row")[2]).toHaveAttribute("data-current", "true");
    act(() => {
      useHub.getState().onMessage({ type: "delta", from_version: 11, to_version: 12, changed: { "queues.heos:heos-1": null } });
    });
    // queue removed: the sheet asks the hub again rather than showing a stale list
    await waitFor(() => expect(calls().filter(([u]) => u.startsWith("/api/queue/"))).toHaveLength(1));
  });
});

describe("QueueSheet refusals and centring", () => {
  it("a refused jump (invalid_argument) reverts the optimistic now-playing, current_index and play state locally and toasts the hub's message", async () => {
    const s = sampleState();
    s.queues = { "heos:heos-1": queue({ current_index: 1 }) };
    boot(s, (url, init) => {
      if (url !== "/api/queue/jump") return undefined;
      const cid = (init?.headers as Record<string, string>)?.["x-correlation-id"];
      return jsonResponse({ ...okAck(cid), ok: false, error: { code: "invalid_argument", message: "That track isn't in the queue any more.", target: null, correlation_id: cid } }, 400);
    });
    render(<QueueSheet open sideId="heos:heos-1" onClose={() => {}} />);
    fireEvent.click(screen.getByRole("button", { name: "Play from All Blues, Miles Davis" }));
    await waitFor(() => expect(useToasts.getState().toasts.some((t) => t.message === "That track isn't in the queue any more.")).toBe(true));
    const st = useHub.getState();
    expect(st.state?.queues?.["heos:heos-1"]?.current_index).toBe(1);
    expect(st.state?.now_playing["heos:heos-1"]?.title).toBe("Blue in Green");
    expect(st.state?.sides["heos:heos-1"]?.play_state).toBe("play");
    expect(st.intents["heos:heos-1"]).toBeUndefined();
  });

  it("centres the current row on open (instant, never smooth) and only once per open", () => {
    const s = sampleState();
    s.queues = { "heos:heos-1": queue({ current_index: 2 }) };
    boot(s);
    const spy = vi.spyOn(Element.prototype, "scrollIntoView").mockImplementation(() => {});
    const { rerender } = render(<QueueSheet open sideId="heos:heos-1" onClose={() => {}} />);
    expect(spy).toHaveBeenCalledWith({ block: "center", behavior: "auto" });
    const calls = spy.mock.calls.length;
    rerender(<QueueSheet open sideId="heos:heos-1" onClose={() => {}} />);
    expect(spy.mock.calls.length).toBe(calls);
    spy.mockRestore();
  });
});

describe("Now Playing → Up next", () => {
  it("the plain-text Up next button under the transport opens the sheet for the active side", async () => {
    const s = sampleState();
    s.queues = { "heos:heos-1": queue() };
    boot(s);
    render(<NowPlaying />);
    const btn = screen.getByTestId("open-queue");
    expect(btn).toHaveTextContent("Up next");
    expect(btn.className).not.toContain("bg-signal");
    fireEvent.click(btn);
    const sheet = await screen.findByTestId("queue-sheet");
    expect(within(sheet).getAllByTestId("queue-row")).toHaveLength(3);
  });
});
