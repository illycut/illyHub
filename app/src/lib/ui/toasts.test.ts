import { TOAST_TTL_MS, toast, useToasts } from "./toasts";

describe("toasts", () => {
  beforeEach(() => useToasts.setState({ toasts: [] }));
  it("keeps at most two, newest replacing oldest, and expires after 4 s", () => {
    useToasts.getState().push("one", 0);
    useToasts.getState().push("two", 100);
    useToasts.getState().push("three", 200);
    expect(useToasts.getState().toasts.map((t) => t.message)).toEqual(["two", "three"]);
    useToasts.getState().expire(100 + TOAST_TTL_MS);
    expect(useToasts.getState().toasts.map((t) => t.message)).toEqual(["three"]);
    const id = toast("four");
    useToasts.getState().dismiss(id);
    expect(useToasts.getState().toasts.map((t) => t.message)).toEqual(["three"]);
  });
});
