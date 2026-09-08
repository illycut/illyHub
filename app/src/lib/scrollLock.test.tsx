import { render } from "@testing-library/react";
import { scrollLockCount, useScrollLock } from "./scrollLock";

function Lock({ on }: { on: boolean }) {
  useScrollLock(on);
  return null;
}

describe("useScrollLock", () => {
  it("adds the body class while active and reference-counts nested locks", () => {
    const a = render(<Lock on />);
    const b = render(<Lock on />);
    expect(document.body.classList.contains("scroll-lock")).toBe(true);
    expect(scrollLockCount()).toBe(2);
    a.unmount();
    expect(document.body.classList.contains("scroll-lock")).toBe(true);
    b.unmount();
    expect(document.body.classList.contains("scroll-lock")).toBe(false);
    const c = render(<Lock on={false} />);
    expect(document.body.classList.contains("scroll-lock")).toBe(false);
    c.unmount();
  });
});
