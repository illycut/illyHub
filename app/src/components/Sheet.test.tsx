import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { Sheet, trapTab } from "./Sheet";

vi.mock("framer-motion", async () => {
  const actual = await vi.importActual<typeof import("framer-motion")>("framer-motion");
  return { ...actual, AnimatePresence: ({ children }: { children: React.ReactNode }) => <>{children}</> };
});

function Host() {
  const [open, setOpen] = useState(false);
  return (
    <>
      <button type="button" onClick={() => setOpen(true)} data-testid="opener">
        Open
      </button>
      <Sheet open={open} onClose={() => setOpen(false)} title="Sheet" testId="sheet">
        <button type="button" data-testid="a">
          A
        </button>
        <button type="button" data-testid="b">
          B
        </button>
      </Sheet>
    </>
  );
}

describe("Sheet focus handling (U3)", () => {
  it("is aria-modal, takes focus on open, cycles Tab inside, and returns focus to the opener on close", async () => {
    render(<Host />);
    const opener = screen.getByTestId("opener");
    opener.focus();
    await userEvent.click(opener);
    const sheet = screen.getByTestId("sheet");
    expect(sheet).toHaveAttribute("aria-modal", "true");
    expect(document.activeElement).toBe(sheet);
    // Tab from the container goes to the first control; Tab past the last wraps to the first
    fireEvent.keyDown(window, { key: "Tab" });
    expect(document.activeElement).toBe(screen.getByTestId("a"));
    screen.getByTestId("b").focus();
    fireEvent.keyDown(window, { key: "Tab" });
    expect(document.activeElement).toBe(screen.getByTestId("a"));
    fireEvent.keyDown(window, { key: "Tab", shiftKey: true });
    expect(document.activeElement).toBe(screen.getByTestId("b"));
    fireEvent.keyDown(window, { key: "Escape" });
    await waitFor(() => expect(screen.queryByTestId("sheet")).toBeNull());
    await waitFor(() => expect(document.activeElement).toBe(opener));
  });

  it("trapTab with no focusable content keeps focus on the root", () => {
    const root = document.createElement("div");
    root.tabIndex = -1;
    document.body.appendChild(root);
    const e = new KeyboardEvent("keydown", { key: "Tab", cancelable: true });
    trapTab(root, e);
    expect(e.defaultPrevented).toBe(true);
    expect(document.activeElement).toBe(root);
    root.remove();
  });
});
