import { GENERIC_KEY, MAX_ENTRIES, readLastTarget, rememberTargets } from "./prefs";

describe("prefs", () => {
  beforeEach(() => localStorage.clear());

  it("one write remembers the content ref and the generic slot; reads by either key; tolerates garbage", () => {
    expect(readLastTarget()).toBeNull();
    const set = vi.spyOn(Storage.prototype, "setItem");
    rememberTargets(["heos:b"], "tidal:album:1");
    expect(set).toHaveBeenCalledTimes(1); // A20: a single storage write
    set.mockRestore();
    expect(readLastTarget()).toEqual(["heos:b"]);
    expect(readLastTarget("tidal:album:1")).toEqual(["heos:b"]);
    rememberTargets(["sonos:a"]);
    expect(readLastTarget(GENERIC_KEY)).toEqual(["sonos:a"]);
    expect(readLastTarget("tidal:album:1")).toEqual(["heos:b"]);
    localStorage.setItem("illyhub.lastTarget.v2", "{not json");
    expect(readLastTarget()).toBeNull();
    rememberTargets(["x"]); // recovers by rewriting
    expect(readLastTarget()).toEqual(["x"]);
  });

  it("caps content entries at MAX_ENTRIES, evicting the least recently written; the generic slot survives", () => {
    rememberTargets(["g"]);
    for (let i = 0; i < MAX_ENTRIES + 5; i++) rememberTargets([`s${i}`], `k${i}`);
    expect(readLastTarget("k0")).toBeNull();
    expect(readLastTarget("k4")).toBeNull();
    expect(readLastTarget("k5")).toEqual(["s5"]);
    expect(readLastTarget(`k${MAX_ENTRIES + 4}`)).toEqual([`s${MAX_ENTRIES + 4}`]);
    expect(readLastTarget()).toEqual([`s${MAX_ENTRIES + 4}`]);
    // re-writing an old key moves it to the front
    rememberTargets(["again"], "k5");
    for (let i = 0; i < 3; i++) rememberTargets([`n${i}`], `n${i}`);
    expect(readLastTarget("k5")).toEqual(["again"]);
  });

  it("survives a throwing storage", () => {
    const spy = vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("blocked");
    });
    expect(readLastTarget()).toBeNull();
    expect(() => rememberTargets(["a"])).not.toThrow();
    spy.mockRestore();
  });
});
