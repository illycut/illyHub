/**
 * Shuffle / repeat (Phase 8, ai-dev #79) on Now Playing: 48px toggles under the transport row with
 * glyph + dot + aria-pressed ("Shuffle" / "Repeat all"), repeat cycling off → all → one, POST
 * /api/playmode with an optimistic patch that reverts on refusal, a permanent caption slot so
 * play/pause never moves, disabled during Sync Play, hidden for stations.
 */
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { axe } from "vitest-axe";
import { NowPlaying } from "./NowPlaying";
import { PlayOptionsRow } from "./PlayOptionsRow";
import { useHub } from "@/lib/hub/store";
import { useToasts } from "@/lib/ui/toasts";
import { PLAY_MODES_OFF_DURING_SYNC } from "@/lib/playMode";
import { jsonResponse, okAck, sampleState, sampleSync } from "@/test/fixtures";
import type { HubState } from "@/lib/hub/types";

vi.mock("framer-motion", async () => {
  const actual = await vi.importActual<typeof import("framer-motion")>("framer-motion");
  return { ...actual, AnimatePresence: ({ children }: { children: React.ReactNode }) => <>{children}</> };
});

const fetchMock = vi.fn();
const fetcher = fetchMock as unknown as typeof fetch;
const calls = () => fetchMock.mock.calls.map((c) => [String(c[0]).replace(/^https?:\/\/[^/]+/, ""), c[1]?.body ? JSON.parse(c[1].body as string) : undefined] as const);

function boot(state: HubState = sampleState()) {
  useHub.getState()._reset();
  useToasts.setState({ toasts: [] });
  fetchMock.mockReset();
  fetchMock.mockImplementation(async (_u: RequestInfo | URL, init?: RequestInit) => jsonResponse(okAck((init?.headers as Record<string, string>)?.["x-correlation-id"])));
  useHub.setState({ _deps: { fetcher, now: () => Date.now(), setTimer: (fn, ms) => setTimeout(fn, ms), clearTimer: (t) => clearTimeout(t as ReturnType<typeof setTimeout>) } });
  useHub.getState().onMessage({ type: "snapshot", version: 10, state });
  useHub.getState().setPhase("open", Date.now());
}

const noop = { onShuffle: () => {}, onRepeat: () => {}, onUpNext: () => {} };

describe("PlayOptionsRow", () => {
  it("48px toggles named 'Shuffle' / 'Repeat …' with aria-pressed and a dot under the glyph when on (shape, not fill alone); repeat cycles; Up next in between; passes axe", async () => {
    const onShuffle = vi.fn();
    const onRepeat = vi.fn();
    const onUpNext = vi.fn();
    const { container, rerender } = render(<PlayOptionsRow mode={{ shuffle: false, repeat: "off" }} modesHidden={false} syncActive={false} onShuffle={onShuffle} onRepeat={onRepeat} onUpNext={onUpNext} />);
    const shuffle = screen.getByTestId("shuffle-toggle");
    expect(shuffle).toHaveAccessibleName("Shuffle");
    expect(shuffle).toHaveAttribute("aria-pressed", "false");
    expect(shuffle.className).toContain("hit-target");
    expect(shuffle.querySelector("[data-on]")).toBeNull();
    fireEvent.click(shuffle);
    expect(onShuffle).toHaveBeenCalledWith(true);
    const repeat = screen.getByTestId("repeat-toggle");
    expect(repeat).toHaveAccessibleName("Repeat");
    fireEvent.click(repeat);
    expect(onRepeat).toHaveBeenCalledWith("all");
    rerender(<PlayOptionsRow mode={{ shuffle: true, repeat: "all" }} modesHidden={false} syncActive={false} onShuffle={onShuffle} onRepeat={onRepeat} onUpNext={onUpNext} />);
    expect(screen.getByTestId("shuffle-toggle")).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByTestId("shuffle-toggle").querySelector("[data-on='true']")).not.toBeNull();
    expect(screen.getByTestId("shuffle-toggle").className).not.toContain("bg-signal");
    expect(screen.getByTestId("repeat-toggle")).toHaveAccessibleName("Repeat all");
    fireEvent.click(screen.getByTestId("repeat-toggle"));
    expect(onRepeat).toHaveBeenLastCalledWith("one");
    rerender(<PlayOptionsRow mode={{ shuffle: true, repeat: "one" }} modesHidden={false} syncActive={false} onShuffle={onShuffle} onRepeat={onRepeat} onUpNext={onUpNext} />);
    expect(screen.getByTestId("repeat-toggle")).toHaveAttribute("data-repeat", "one");
    expect(screen.getByTestId("repeat-toggle")).toHaveAccessibleName("Repeat one");
    // the repeat-one glyph carries a legible digit
    expect(screen.getByTestId("repeat-toggle").querySelector("svg text")?.textContent).toBe("1");
    fireEvent.click(screen.getByTestId("repeat-toggle"));
    expect(onRepeat).toHaveBeenLastCalledWith("off");
    fireEvent.click(screen.getByTestId("open-queue"));
    expect(onUpNext).toHaveBeenCalled();
    expect(await axe(container)).toHaveNoViolations();
  });

  it("the caption slot is always rendered with a fixed min-height so the row never changes height when Sync Play starts (U3)", () => {
    const { rerender } = render(<PlayOptionsRow mode={{ shuffle: false, repeat: "off" }} modesHidden={false} syncActive={false} {...noop} />);
    const slot = screen.getByTestId("play-modes-caption");
    expect(slot.className).toContain("min-h-[var(--type-micro-line)]");
    expect(slot).toHaveTextContent("");
    expect(slot).toHaveAttribute("aria-hidden", "true");
    const before = screen.getByTestId("play-options").childElementCount;
    rerender(<PlayOptionsRow mode={{ shuffle: false, repeat: "off" }} modesHidden={false} syncActive {...noop} />);
    const after = screen.getByTestId("play-modes-caption");
    expect(after).toBe(slot); // same node, same box
    expect(after).toHaveTextContent(PLAY_MODES_OFF_DURING_SYNC);
    expect(after).not.toHaveAttribute("aria-hidden");
    expect(screen.getByTestId("play-options").childElementCount).toBe(before);
  });

  it("Sync Play: toggles disabled and described by the caption; stations: toggles hidden, Up next stays", () => {
    const { rerender } = render(<PlayOptionsRow mode={{ shuffle: false, repeat: "off" }} modesHidden={false} syncActive {...noop} />);
    expect(screen.getByTestId("shuffle-toggle")).toBeDisabled();
    expect(screen.getByTestId("repeat-toggle")).toBeDisabled();
    expect(screen.getByTestId("shuffle-toggle")).toHaveAttribute("aria-describedby", "play-modes-caption");
    expect(screen.getByTestId("open-queue")).toBeEnabled();
    rerender(<PlayOptionsRow mode={{ shuffle: false, repeat: "off" }} modesHidden syncActive={false} {...noop} />);
    expect(screen.queryByTestId("shuffle-toggle")).toBeNull();
    expect(screen.queryByTestId("repeat-toggle")).toBeNull();
    expect(screen.getByTestId("play-modes-caption")).toHaveTextContent("");
    expect(screen.getByTestId("open-queue")).toBeInTheDocument();
  });
});

describe("Now Playing play modes", () => {
  it("reads side.play_mode, posts /api/playmode with only the changed field, and patches optimistically", async () => {
    const s = sampleState();
    s.sides["heos:heos-1"]!.play_mode = { shuffle: false, repeat: "all" };
    boot(s);
    render(<NowPlaying />);
    expect(screen.getByTestId("repeat-toggle")).toHaveAccessibleName("Repeat all");
    fireEvent.click(screen.getByTestId("shuffle-toggle"));
    await waitFor(() => expect(calls().some(([u, b]) => u === "/api/playmode" && b?.target === "heos:heos-1" && b?.shuffle === true && b?.repeat === undefined)).toBe(true));
    expect(useHub.getState().state?.sides["heos:heos-1"]?.play_mode).toEqual({ shuffle: true, repeat: "all" });
    expect(screen.getByTestId("shuffle-toggle")).toHaveAttribute("aria-pressed", "true");
    fireEvent.click(screen.getByTestId("repeat-toggle"));
    await waitFor(() => expect(calls().some(([u, b]) => u === "/api/playmode" && b?.repeat === "one" && b?.shuffle === undefined)).toBe(true));
    expect(screen.getByTestId("repeat-toggle")).toHaveAccessibleName("Repeat one");
  });

  it("during Sync Play the toggles are disabled with the caption; a station hides them; a refused change (sync_active) reverts the mode and toasts the hub's message", async () => {
    const s = sampleState();
    s.sync = sampleSync();
    boot(s);
    render(<NowPlaying />);
    expect(screen.getByTestId("shuffle-toggle")).toBeDisabled();
    expect(screen.getByTestId("play-modes-caption")).toHaveTextContent(PLAY_MODES_OFF_DURING_SYNC);

    boot(sampleState());
    useHub.getState().selectSide("sonos:sonos-gK");
    render(<NowPlaying />);
    expect(screen.queryByTestId("shuffle-toggle")).toBeNull();
    expect(screen.getAllByTestId("open-queue").length).toBeGreaterThan(0);

    const s3 = sampleState();
    s3.sides["heos:heos-1"]!.play_mode = { shuffle: false, repeat: "off" };
    boot(s3);
    fetchMock.mockImplementation(async (_u: RequestInfo | URL, init?: RequestInit) => {
      const cid = (init?.headers as Record<string, string>)?.["x-correlation-id"];
      return jsonResponse({ ...okAck(cid), ok: false, error: { code: "sync_active", message: "Stop Sync Play first.", target: null, correlation_id: cid } }, 409);
    });
    render(<NowPlaying />);
    fireEvent.click(screen.getAllByTestId("shuffle-toggle")[0]!);
    // optimistic on, then reverted by the refusal
    await waitFor(() => expect(useToasts.getState().toasts.some((t) => t.message === "Stop Sync Play first.")).toBe(true));
    expect(useHub.getState().state?.sides["heos:heos-1"]?.play_mode).toEqual({ shuffle: false, repeat: "off" });
    expect(screen.getAllByTestId("shuffle-toggle")[0]).toHaveAttribute("aria-pressed", "false");
  });
});
