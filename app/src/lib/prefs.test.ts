import { readLastTarget, writeLastTarget } from "./prefs";

describe("prefs", () => {
  beforeEach(() => localStorage.clear());
  it("round-trips the last target per content key and tolerates garbage", () => {
    expect(readLastTarget()).toBeNull();
    writeLastTarget(["sonos:a"]);
    writeLastTarget(["heos:b"], "album:1");
    expect(readLastTarget()).toEqual(["sonos:a"]);
    expect(readLastTarget("album:1")).toEqual(["heos:b"]);
    localStorage.setItem("illyhub.lastTarget.v1", "{not json");
    expect(readLastTarget()).toBeNull();
    writeLastTarget(["x"]); // recovers by rewriting
    expect(readLastTarget()).toEqual(["x"]);
  });
  it("survives a throwing storage", () => {
    const spy = vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("blocked");
    });
    expect(readLastTarget()).toBeNull();
    expect(() => writeLastTarget(["a"])).not.toThrow();
    spy.mockRestore();
  });
});
