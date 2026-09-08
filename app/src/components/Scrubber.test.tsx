import { fireEvent, render, screen } from "@testing-library/react";
import { Scrubber } from "./Scrubber";

const pos = (ms: number, conf = 0.9) => ({ position_ms: ms, reported_at: new Date(1_000_000).toISOString(), confidence: conf });

function mockTrack(el: HTMLElement) {
  vi.spyOn(el, "getBoundingClientRect").mockReturnValue({ left: 0, width: 200, top: 0, height: 4, right: 200, bottom: 4, x: 0, y: 0, toJSON: () => ({}) } as DOMRect);
}
const track = (slider: HTMLElement) => slider.querySelector("div.relative.w-full") as HTMLElement;

describe("Scrubber", () => {
  it("renders interpolated elapsed and total time on the hub clock", () => {
    render(<Scrubber position={pos(60_000)} playState="play" durationMs={337_000} seekable onSeek={() => {}} now={() => 1_002_500} />);
    expect(screen.getByTestId("elapsed")).toHaveTextContent("1:02");
    expect(screen.getByTestId("total")).toHaveTextContent("5:37");
    expect(screen.getByTestId("scrubber")).toHaveAttribute("data-seekable", "true");
    expect(screen.getByTestId("scrub-thumb")).toBeInTheDocument();
  });

  it("read-only when the side cannot seek: full-opacity track and timecodes, no thumb, focusable, inert", () => {
    const onSeek = vi.fn();
    render(<Scrubber position={pos(0, 1)} playState="play" durationMs={200_000} seekable={false} onSeek={onSeek} now={() => 1_000_000} />);
    const el = screen.getByTestId("scrubber");
    expect(el.className).not.toContain("opacity-40");
    expect(el).toHaveAttribute("data-seekable", "false");
    const slider = screen.getByRole("slider");
    expect(slider).toHaveAttribute("aria-readonly", "true");
    expect(slider).toHaveAttribute("tabindex", "0");
    expect(screen.queryByTestId("scrub-thumb")).toBeNull();
    fireEvent.pointerDown(slider, { clientX: 100, pointerId: 1 });
    fireEvent.pointerUp(slider);
    fireEvent.keyDown(slider, { key: "ArrowRight" });
    expect(onSeek).not.toHaveBeenCalled();
    expect(screen.queryByTestId("cue-bubble")).toBeNull();
    expect(screen.getByTestId("elapsed")).toHaveTextContent("0:00");
  });

  it("shows a cue bubble while dragging and seeks once on release", () => {
    const onSeek = vi.fn();
    render(<Scrubber position={pos(0, 1)} playState="pause" durationMs={200_000} seekable onSeek={onSeek} now={() => 1_000_000} />);
    const slider = screen.getByRole("slider");
    mockTrack(track(slider));
    fireEvent.pointerDown(slider, { clientX: 50, pointerId: 1 });
    expect(screen.getByTestId("cue-bubble")).toHaveTextContent("0:50");
    fireEvent.pointerMove(slider, { clientX: 100, pointerId: 1 });
    expect(screen.getByTestId("cue-bubble")).toHaveTextContent("1:40");
    expect(onSeek).not.toHaveBeenCalled();
    fireEvent.pointerUp(slider);
    expect(onSeek).toHaveBeenCalledTimes(1);
    expect(onSeek).toHaveBeenCalledWith(100_000);
    expect(screen.queryByTestId("cue-bubble")).toBeNull();
    expect(screen.getByText("Seeked to 1:40")).toBeInTheDocument();
    expect(screen.getByTestId("elapsed")).toHaveTextContent("1:40"); // committed value holds until a new hub position
  });

  it("a cancelled pointer discards the scrub without seeking", () => {
    const onSeek = vi.fn();
    render(<Scrubber position={pos(10_000, 1)} playState="pause" durationMs={200_000} seekable onSeek={onSeek} />);
    const slider = screen.getByRole("slider");
    mockTrack(track(slider));
    fireEvent.pointerDown(slider, { clientX: 150, pointerId: 1 });
    expect(screen.getByTestId("cue-bubble")).toBeInTheDocument();
    fireEvent.pointerCancel(slider);
    expect(screen.queryByTestId("cue-bubble")).toBeNull();
    expect(onSeek).not.toHaveBeenCalled();
    expect(screen.getByTestId("elapsed")).toHaveTextContent("0:10");
  });

  it("survives setPointerCapture throwing (WebKit synthetic pointers)", () => {
    const onSeek = vi.fn();
    render(<Scrubber position={pos(0, 1)} playState="pause" durationMs={200_000} seekable onSeek={onSeek} />);
    const slider = screen.getByRole("slider");
    mockTrack(track(slider));
    (slider as HTMLElement & { setPointerCapture: () => void }).setPointerCapture = () => {
      throw new DOMException("InvalidPointerId");
    };
    fireEvent.pointerDown(slider, { clientX: 100, pointerId: 7 });
    fireEvent.pointerUp(slider);
    expect(onSeek).toHaveBeenCalledWith(100_000);
  });

  it("keyboard seeks by 5 s / 30 s with shift, chaining from the committed value", () => {
    const onSeek = vi.fn();
    render(<Scrubber position={pos(60_000, 1)} playState="pause" durationMs={200_000} seekable onSeek={onSeek} />);
    const slider = screen.getByRole("slider");
    fireEvent.keyDown(slider, { key: "ArrowRight" });
    expect(onSeek).toHaveBeenLastCalledWith(65_000);
    fireEvent.keyDown(slider, { key: "ArrowLeft", shiftKey: true });
    expect(onSeek).toHaveBeenLastCalledWith(35_000);
  });
});
