import { hubDegraded, linkedVolumeLevels, liveSideIds, playersList, resolveActiveSide, sideForPlayer, sidesList, zoneDotModel, zoneDotsLabel, zonesForSide } from "./selectors";
import { player, sampleState, side } from "@/test/fixtures";

describe("selectors", () => {
  const s = sampleState();
  it("lists sides/players sorted, finds live ones, and memoizes on the collection object", () => {
    expect(sidesList(s).map((x) => x.name)).toEqual(["Kitchen + 1", "Living Room Amp"]);
    expect(liveSideIds(s)).toEqual(["heos:heos-1"]);
    expect(sidesList(null)).toEqual([]);
    expect(sidesList(s)).toBe(sidesList({ ...s, positions: {} })); // positions changed, sides identical → same array
    expect(playersList(s)).toBe(playersList(s));
  });
  it("resolves the active side: chosen > playing with metadata > any with metadata > playing > first", () => {
    expect(resolveActiveSide(s, "sonos:sonos-gK")?.id).toBe("sonos:sonos-gK");
    expect(resolveActiveSide(s, "missing")?.id).toBe("heos:heos-1");
    expect(resolveActiveSide(s, null)?.id).toBe("heos:heos-1");
    // heos playing but without metadata, sonos idle with metadata → sonos
    const noNp = { ...s, now_playing: { "sonos:sonos-gK": s.now_playing["sonos:sonos-gK"]! } };
    expect(resolveActiveSide(noNp, null)?.id).toBe("sonos:sonos-gK");
    // nobody has metadata → first playing
    expect(resolveActiveSide({ ...s, now_playing: {} }, null)?.id).toBe("heos:heos-1");
    const quiet = { ...s, now_playing: {}, sides: { ...s.sides, "heos:heos-1": { ...s.sides["heos:heos-1"]!, play_state: "stop" as const } } };
    expect(resolveActiveSide(quiet, null)?.id).toBe("sonos:sonos-gK");
    expect(resolveActiveSide(null, null)).toBeNull();
  });
  it("maps players to sides and sides to zones", () => {
    expect(sideForPlayer(s, "sonos-P")?.id).toBe("sonos:sonos-gK");
    expect(sideForPlayer(s, "nope")).toBeNull();
    expect(zonesForSide(s, s.sides["heos:heos-1"]!).map((z) => z.key)).toEqual(["main"]);
    expect(zonesForSide(s, s.sides["sonos:sonos-gK"]!)).toEqual([]);
  });
  it("zone dot cluster: live/online per dot, count chip past four live, label mentions offline", () => {
    const sides = ["a", "b", "c", "d", "e"].map((id) => side(id, id, "sonos", [id]));
    const players = { a: player("a", "a", "sonos", { online: false }) };
    const m = zoneDotModel(sides, ["a", "c"], players);
    expect(m).toEqual({ kind: "dots", dots: [{ live: true, online: false }, { live: false, online: true }, { live: true, online: true }, { live: false, online: true }, { live: false, online: true }], live: 2, offline: 1 });
    expect(zoneDotsLabel(m)).toBe("2 rooms playing, 1 offline");
    expect(zoneDotModel(sides, ["a", "b", "c", "d"]).kind).toBe("dots");
    const c = zoneDotModel(sides, ["a", "b", "c", "d", "e"]);
    expect(c).toEqual({ kind: "count", count: 5, offline: 0 });
    expect(zoneDotsLabel(c)).toBe("5 rooms playing");
    expect(zoneDotsLabel(zoneDotModel([sides[0]!], ["a"]))).toBe("1 room playing");
  });
  it("degraded when any adapter is reconnecting or disconnected, not when disabled", () => {
    expect(hubDegraded(s)).toBe(false);
    expect(hubDegraded({ ...s, connections: { ...s.connections, heos: { state: "reconnecting", last_error: null, since: "" } } })).toBe(true);
    expect(hubDegraded({ ...s, connections: { ...s.connections, heos: { state: "disabled", last_error: null, since: "" } } })).toBe(false);
    expect(hubDegraded(null)).toBe(false);
  });
  it("linked volume levels mirror the hub rule: loudest online is master, ratios kept, quiet rooms floor at 1", () => {
    const players = { a: player("a", "a", "sonos", { volume: 100 }), b: player("b", "b", "sonos", { volume: 1 }), c: player("c", "c", "sonos", { volume: 50 }), d: player("d", "d", "sonos", { volume: 90, online: false }) };
    expect(linkedVolumeLevels(players, { delta: -50 })).toEqual({ a: 50, b: 1, c: 25 });
    expect(linkedVolumeLevels(players, { level: 100 })).toEqual({ a: 100, b: 1, c: 50 });
    expect(linkedVolumeLevels(players, { level: 0 })).toEqual({ a: 0, b: 0, c: 0 });
    const zeros = { a: player("a", "a", "sonos", { volume: 0 }), b: player("b", "b", "sonos", { volume: 0 }) };
    expect(linkedVolumeLevels(zeros, { level: 30 })).toEqual({ a: 30, b: 30 });
    expect(linkedVolumeLevels({}, { level: 30 })).toEqual({});
  });
});
