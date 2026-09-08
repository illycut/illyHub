import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { TransportRow } from "./TransportRow";

const all = { supports_seek: true, supports_next: true, supports_prev: true };

describe("TransportRow", () => {
  it("renders five controls with 56px hit targets and a 64px primary", () => {
    render(<TransportRow playState="play" caps={all} onPrev={() => {}} onBack15={() => {}} onToggle={() => {}} onForward15={() => {}} onNext={() => {}} />);
    const buttons = screen.getAllByRole("button");
    expect(buttons).toHaveLength(5);
    expect(screen.getByLabelText("Back 15 seconds").className).toContain("h-target-lg");
    expect(screen.getByTestId("play-toggle").className).toContain("h-play");
    expect(screen.getByRole("button", { name: "Pause" })).toHaveAttribute("aria-pressed", "true");
  });

  it("disables ±15 without seek and next/prev per source capability", async () => {
    const fns = { onPrev: vi.fn(), onBack15: vi.fn(), onToggle: vi.fn(), onForward15: vi.fn(), onNext: vi.fn() };
    render(<TransportRow playState="pause" caps={{ supports_seek: false, supports_next: true, supports_prev: false }} {...fns} />);
    expect(screen.getByLabelText("Back 15 seconds")).toBeDisabled();
    expect(screen.getByLabelText("Forward 15 seconds")).toBeDisabled();
    expect(screen.getByLabelText("Previous track")).toBeDisabled();
    expect(screen.getByLabelText("Next track")).toBeEnabled();
    await userEvent.click(screen.getByLabelText("Next track"));
    await userEvent.click(screen.getByRole("button", { name: "Play" }));
    expect(fns.onNext).toHaveBeenCalledTimes(1);
    expect(fns.onToggle).toHaveBeenCalledTimes(1);
    expect(fns.onBack15).not.toHaveBeenCalled();
  });
});
