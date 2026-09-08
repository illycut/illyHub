import { act, fireEvent, render, screen } from "@testing-library/react";
import { ZoneDots } from "./ZoneDots";
import { ServiceBadge, serviceLabel } from "./ServiceBadge";
import { Slider } from "./Slider";
import { Toasts } from "./Toasts";
import { HubBanner } from "./Banner";
import { SyncChip } from "./SyncChip";
import { ArtSkeleton, ConnectCard, EmptyState, Skeleton, TextSkeleton } from "./Skeleton";
import { useToasts } from "@/lib/ui/toasts";
import { useHub } from "@/lib/hub/store";

describe("ZoneDots", () => {
  it("renders one dot per side: amber live, dim idle, tertiary offline; label counts both", () => {
    render(<ZoneDots model={{ kind: "dots", dots: [{ live: true, online: true }, { live: false, online: true }, { live: false, online: false }], live: 1, offline: 1 }} />);
    const dots = screen.getByRole("img").querySelectorAll("span[data-live]");
    expect(dots).toHaveLength(3);
    expect(dots[0]!.className).toContain("bg-signal");
    expect(dots[1]!.className).toContain("bg-signal-dim");
    expect(dots[2]!.className).toContain("bg-tertiary");
    expect(screen.getByRole("img")).toHaveAccessibleName("1 room playing, 1 offline");
  });
  it("count chip", () => {
    render(<ZoneDots model={{ kind: "count", count: 5, offline: 0 }} />);
    expect(screen.getByText("5 rooms")).toHaveAccessibleName("5 rooms playing");
  });
});

describe("ServiceBadge", () => {
  it("labels the source and is not a button", () => {
    render(<ServiceBadge source="tidal" />);
    expect(screen.getByRole("img", { name: "Tidal" })).toBeInTheDocument();
    expect(screen.queryByRole("button")).toBeNull();
    expect(serviceLabel("ytmusic")).toBe("YouTube Music");
    expect(serviceLabel(null)).toBeNull();
  });
  it("renders the labelled pill and unknown sources", () => {
    const { container } = render(<ServiceBadge source="pandora" withLabel />);
    expect(screen.getByText("Pandora")).toBeInTheDocument();
    expect(container.querySelector(".bg-badge-pandora")).not.toBeNull();
    render(<ServiceBadge source="qobuz" />);
    expect(screen.getByRole("img", { name: "qobuz" })).toBeInTheDocument();
    const { container: empty } = render(<ServiceBadge source={null} />);
    expect(empty).toBeEmptyDOMElement();
  });
});

describe("Slider", () => {
  it("exposes slider semantics and moves with the keyboard, committing on each step", () => {
    const onChange = vi.fn();
    const onCommit = vi.fn();
    render(<Slider label="Kitchen volume" value={20} onChange={onChange} onCommit={onCommit} />);
    const s = screen.getByRole("slider", { name: "Kitchen volume" });
    expect(s).toHaveAttribute("aria-valuenow", "20");
    fireEvent.keyDown(s, { key: "ArrowRight" });
    expect(onChange).toHaveBeenLastCalledWith(21);
    fireEvent.keyDown(s, { key: "ArrowDown", shiftKey: true });
    expect(onCommit).toHaveBeenLastCalledWith(10);
    fireEvent.keyDown(s, { key: "End" });
    expect(onCommit).toHaveBeenLastCalledWith(100);
    fireEvent.keyDown(s, { key: "Home" });
    expect(onCommit).toHaveBeenLastCalledWith(0);
    fireEvent.keyDown(s, { key: "a" });
    expect(onCommit).toHaveBeenCalledTimes(4);
  });
  it("drags with pointer events, calling onChange during and onCommit on release; cancel discards", () => {
    const onChange = vi.fn();
    const onCommit = vi.fn();
    render(<Slider label="v" value={0} onChange={onChange} onCommit={onCommit} testId="s" />);
    const s = screen.getByTestId("s");
    const track = s.querySelector("div.relative.w-full") as HTMLElement;
    vi.spyOn(track, "getBoundingClientRect").mockReturnValue({ left: 0, width: 100, top: 0, height: 4, right: 100, bottom: 4, x: 0, y: 0, toJSON: () => ({}) } as DOMRect);
    (s as HTMLElement & { setPointerCapture: () => void }).setPointerCapture = () => {
      throw new Error("nope");
    };
    fireEvent.pointerDown(s, { clientX: 25, pointerId: 1 });
    fireEvent.pointerMove(s, { clientX: 75, pointerId: 1 });
    expect(onChange.mock.calls.map((c) => c[0])).toEqual([25, 75]);
    fireEvent.pointerUp(s);
    expect(onCommit).toHaveBeenCalledWith(75);
    fireEvent.pointerDown(s, { clientX: 10, pointerId: 2 });
    fireEvent.pointerCancel(s);
    expect(onCommit).toHaveBeenCalledTimes(1);
  });
  it("is inert when disabled and uses token-sized thumb/track classes", () => {
    const onChange = vi.fn();
    const { container } = render(<Slider label="v" value={5} disabled onChange={onChange} testId="s" />);
    const s = screen.getByTestId("s");
    expect(s).toHaveAttribute("aria-disabled", "true");
    expect(s.className).toContain("opacity-40");
    fireEvent.keyDown(s, { key: "ArrowRight" });
    fireEvent.pointerDown(s, { clientX: 25, pointerId: 1 });
    expect(onChange).not.toHaveBeenCalled();
    expect(container.querySelector(".h-slider-thumb.w-slider-thumb")).not.toBeNull();
    expect(container.querySelector(".h-slider-track")).not.toBeNull();
  });
});

describe("SyncChip", () => {
  const base = { side_ids: ["a", "b"], drift_ms: 0, last_correction_at: null };
  it("renders nothing when idle and the three live states with glyph + text", () => {
    const { container, rerender } = render(<SyncChip sync={{ status: "idle", ...base }} />);
    expect(container).toBeEmptyDOMElement();
    rerender(<SyncChip sync={{ status: "locked", ...base }} />);
    expect(screen.getByRole("status")).toHaveTextContent("Synced");
    expect(screen.getByTestId("sync-chip").querySelector(".bg-sync-locked")).not.toBeNull();
    rerender(<SyncChip sync={{ status: "drifting", ...base }} />);
    expect(screen.getByRole("status")).toHaveTextContent("Adjusting");
    rerender(<SyncChip sync={{ status: "priming", ...base }} />);
    expect(screen.getByRole("status")).toHaveTextContent("Starting");
    rerender(<SyncChip sync={null} />);
    expect(container).toBeEmptyDOMElement();
  });
  it("pulses once per correction event and offers retry when lost", () => {
    const onRetry = vi.fn();
    const { rerender } = render(<SyncChip sync={{ status: "drifting", ...base }} />);
    expect(screen.getByTestId("sync-chip").querySelector(".pulse-once")).toBeNull();
    rerender(<SyncChip sync={{ status: "drifting", ...base, last_correction_at: "2026-09-07T00:00:01Z" }} />);
    expect(screen.getByTestId("sync-chip").querySelector(".pulse-once")).not.toBeNull();
    rerender(<SyncChip sync={{ status: "lost", ...base }} onRetry={onRetry} />);
    fireEvent.click(screen.getByRole("button", { name: /Sync lost/ }));
    expect(onRetry).toHaveBeenCalled();
  });
});

describe("Toasts", () => {
  beforeEach(() => useToasts.setState({ toasts: [] }));
  it("renders toasts in a polite live region and expires them", () => {
    vi.useFakeTimers();
    render(<Toasts />);
    act(() => {
      useToasts.getState().push("Volume didn't change on Kitchen", { now: Date.now() });
    });
    expect(screen.getByRole("status")).toHaveTextContent("Volume didn't change on Kitchen");
    expect(screen.getByRole("status").parentElement).toHaveAttribute("aria-live", "polite");
    act(() => {
      vi.advanceTimersByTime(4500);
    });
    expect(screen.queryByRole("status")).toBeNull();
    vi.useRealTimers();
  });
});

describe("HubBanner", () => {
  beforeEach(() => useHub.getState()._reset());
  it("stays hidden while the first connection is pending, shows after a disconnect with the exact copy", () => {
    vi.useFakeTimers();
    const { rerender } = render(<HubBanner />);
    expect(screen.queryByRole("alert")).toBeNull();
    act(() => {
      useHub.getState().setPhase("open", 1000);
      useHub.getState().setPhase("closed", new Date(2026, 0, 1, 10, 20, 30).getTime());
    });
    rerender(<HubBanner />);
    expect(screen.getByRole("alert")).toHaveTextContent("Can't reach the hub. Check that the Mac is on the network.");
    expect(screen.getByText(/Last attempt 10:20:30/)).toBeInTheDocument();
    act(() => {
      useHub.getState().setPhase("open", 5000);
    });
    expect(screen.queryByRole("alert")).toBeNull();
    vi.useRealTimers();
  });
  it("appears after three seconds when the hub never answers", () => {
    vi.useFakeTimers();
    render(<HubBanner />);
    act(() => {
      useHub.getState().setPhase("connecting", 1);
      vi.advanceTimersByTime(3500);
    });
    expect(screen.getByRole("alert")).toBeInTheDocument();
    vi.useRealTimers();
  });
});

describe("Skeletons", () => {
  it("render without shimmer, hold the square, and the connect card carries the spec copy", () => {
    const { container } = render(
      <>
        <Skeleton className="h-4" />
        <ArtSkeleton />
        <TextSkeleton lines={3} />
        <EmptyState>Play something and it lands here.</EmptyState>
        <ConnectCard service="Tidal" />
      </>,
    );
    expect(container.querySelector(".aspect-square")).not.toBeNull();
    expect(container.innerHTML).not.toContain("shimmer");
    expect(container.querySelectorAll("[data-skeleton]").length).toBe(3);
    expect(screen.getByText("Play something and it lands here.")).toBeInTheDocument();
    expect(screen.getByText("Connect Tidal")).toBeInTheDocument();
  });
});
