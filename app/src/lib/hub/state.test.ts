import { applyDelta, emptyState } from "./state";
import { sampleState } from "@/test/fixtures";

describe("applyDelta", () => {
  it("replaces depth-two paths with the full value and deletes on null", () => {
    const s = sampleState();
    const next = applyDelta(s, {
      type: "delta",
      from_version: 10,
      to_version: 11,
      changed: {
        "players.heos-1": { ...s.players["heos-1"], volume: 77 },
        "positions.sonos:sonos-gK": null,
        "sides.new": { ...s.sides["heos:heos-1"], id: "new", name: "New" },
      },
    });
    expect(next).not.toBeNull();
    expect(next!.version).toBe(11);
    expect(next!.players["heos-1"]!.volume).toBe(77);
    expect(next!.positions["sonos:sonos-gK"]).toBeUndefined();
    expect(next!.sides["new"]!.name).toBe("New");
    // untouched collections keep identity, touched ones are copied
    expect(next!.zones).toBe(s.zones);
    expect(next!.players).not.toBe(s.players);
    expect(s.players["heos-1"]!.volume).toBe(40);
  });

  it("applies the depth-one sync path", () => {
    const s = sampleState();
    const next = applyDelta(s, {
      type: "delta",
      from_version: 10,
      to_version: 11,
      changed: { sync: { status: "locked", side_ids: ["a", "b"], drift_ms: 120, last_correction_at: null } },
    });
    expect(next!.sync.status).toBe("locked");
  });

  it("returns null on a version gap so the caller resyncs", () => {
    const s = sampleState();
    expect(applyDelta(s, { type: "delta", from_version: 9, to_version: 11, changed: {} })).toBeNull();
  });

  it("ignores unknown collections and malformed paths", () => {
    const s = sampleState();
    const next = applyDelta(s, { type: "delta", from_version: 10, to_version: 11, changed: { "bogus.x": 1, nope: 2, version: 99 } });
    expect(next!.version).toBe(11);
    expect(next!.players).toBe(s.players);
  });

  it("emptyState is a valid, empty HubState", () => {
    const e = emptyState();
    expect(e.version).toBe(0);
    expect(Object.keys(e.sides)).toHaveLength(0);
    expect(e.sync.status).toBe("idle");
  });
});
