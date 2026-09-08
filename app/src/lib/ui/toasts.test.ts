import { TOAST_ACTION_TTL_MS, TOAST_TTL_MS, toast, ttlOf, useToasts } from "./toasts";

describe("toasts", () => {
  beforeEach(() => useToasts.setState({ toasts: [] }));

  it("keeps at most two, newest replacing oldest, and expires after 4 s", () => {
    useToasts.getState().push("one", { now: 0 });
    useToasts.getState().push("two", { now: 100 });
    useToasts.getState().push("three", { now: 200 });
    expect(useToasts.getState().toasts.map((t) => t.message)).toEqual(["two", "three"]);
    useToasts.getState().expire(100 + TOAST_TTL_MS);
    expect(useToasts.getState().toasts.map((t) => t.message)).toEqual(["three"]);
    const id = toast("four");
    useToasts.getState().dismiss(id);
    expect(useToasts.getState().toasts.map((t) => t.message)).toEqual(["three"]);
  });

  it("a toast with an action lives longer so it can be tapped", () => {
    useToasts.getState().push("plain", { now: 0 });
    useToasts.getState().push("act", { now: 0, action: { label: "Connect Tidal", href: "/settings?link=tidal" } });
    const [plain, act] = useToasts.getState().toasts;
    expect(ttlOf(plain!)).toBe(TOAST_TTL_MS);
    expect(ttlOf(act!)).toBe(TOAST_ACTION_TTL_MS);
    useToasts.getState().expire(TOAST_TTL_MS + 1);
    expect(useToasts.getState().toasts.map((t) => t.message)).toEqual(["act"]);
    expect(useToasts.getState().toasts[0]?.action).toEqual({ label: "Connect Tidal", href: "/settings?link=tidal" });
    useToasts.getState().expire(TOAST_ACTION_TTL_MS + 1);
    expect(useToasts.getState().toasts).toEqual([]);
  });

  it("expire returns the same array when nothing expired", () => {
    useToasts.getState().push("x", { now: 0 });
    const before = useToasts.getState().toasts;
    useToasts.getState().expire(10);
    expect(useToasts.getState().toasts).toBe(before);
  });
});
