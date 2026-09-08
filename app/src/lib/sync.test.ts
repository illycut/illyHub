import { describe, expect, it } from "vitest";
import {
  chipModel,
  isSyncActive,
  isSyncMirrored,
  joinRooms,
  mirroredSideIds,
  offerContent,
  offerSuppressed,
  syncButtonLabel,
  syncEligibility,
  syncLabel,
  syncLabelShort,
  syncNote,
  syncOffer,
  syncSideIds,
  transportTargetFor,
  STATIONS_CANT_SYNC,
  SYNC_UNSUPPORTED_TOAST,
} from "./sync";
import { idleSync, sampleState, sampleSync, side } from "@/test/fixtures";
import type { Detail, HistoryItem } from "./hub/library";

const tidal = { service: "tidal", kind: "album", id: "a-1" } as const;
const pandora = { service: "pandora", kind: "station", id: "s-1" } as const;
const ytPlaylist = { service: "ytmusic", kind: "playlist", id: "p-1" } as const;
const tidalTrack = { service: "tidal", kind: "track", id: "1" } as const;

describe("syncEligibility (picker button rule)", () => {
  const sides = sampleState().sides;
  it("Tidal + one HEOS + one Sonos → Sync Play with both side ids", () => {
    expect(syncEligibility(["heos:heos-1", "sonos:sonos-gK"], sides, tidal)).toEqual({ mode: "sync", heos: ["heos:heos-1"], sonos: ["sonos:sonos-gK"] });
  });
  it("Tidal + one vendor → plain Play, no Sync Play shown", () => {
    expect(syncEligibility(["heos:heos-1"], sides, tidal)).toEqual({ mode: "play", syncReason: null });
    expect(syncEligibility(["sonos:sonos-gK"], sides, tidal)).toEqual({ mode: "play", syncReason: null });
    expect(syncEligibility([], sides, tidal)).toEqual({ mode: "play", syncReason: null });
  });
  it("non-Tidal + both vendors → plain Play with a disabled Sync Play and the reason", () => {
    expect(syncEligibility(["heos:heos-1", "sonos:sonos-gK"], sides, ytPlaylist)).toEqual({ mode: "play", syncReason: SYNC_UNSUPPORTED_TOAST });
    // a station is not merely non-Tidal: Pandora picks per room, so the picker says that instead (Phase 5)
    expect(syncEligibility(["heos:heos-1", "sonos:sonos-gK"], sides, pandora)).toEqual({ mode: "play", syncReason: STATIONS_CANT_SYNC });
  });
  it("passes every selected side per vendor, in selection order (the hub groups them)", () => {
    const more = { ...sides, "heos:heos-2": side("heos:heos-2", "Den", "heos", ["heos-2"]) };
    expect(syncEligibility(["heos:heos-2", "sonos:sonos-gK", "heos:heos-1"], more, tidal)).toEqual({
      mode: "sync",
      heos: ["heos:heos-2", "heos:heos-1"],
      sonos: ["sonos:sonos-gK"],
    });
  });
  it("button copy names the room count past two; the note names the rooms with 'and' and ends honestly", () => {
    expect(syncButtonLabel(2)).toBe("Sync Play");
    expect(syncButtonLabel(3)).toBe("Sync Play in 3 rooms");
    expect(syncNote(["Living Room Amp"], ["Kitchen + Patio"])).toBe("Living Room Amp and Kitchen + Patio, together. Close, not perfect.");
    expect(syncNote(["Den", "Living Room Amp"], ["Kitchen"])).toBe("Den, Living Room Amp and Kitchen, together. Close, not perfect.");
    expect(joinRooms([])).toBe("");
    expect(joinRooms(["A"])).toBe("A");
  });
});

describe("chipModel (§6.7): one glyph whose shape and tone carry the state", () => {
  it("hides when idle, stopped, or absent", () => {
    expect(chipModel(idleSync())).toBeNull();
    expect(chipModel(sampleSync({ status: "stopped" }))).toBeNull();
    expect(chipModel(null)).toBeNull();
    expect(chipModel(undefined)).toBeNull();
  });
  it("Starting = dotted ring, Synced = filled circle, Adjusting = half circle, Sync lost = ring", () => {
    for (const status of ["resolving", "priming", "verifying", "starting"] as const) {
      expect(chipModel(sampleSync({ status }))).toEqual({ text: "Starting", tone: "text-sync-drift", shape: "dotted", lost: false });
    }
    expect(chipModel(sampleSync({ status: "locked" }))).toEqual({ text: "Synced", tone: "text-sync-locked", shape: "filled", lost: false });
    expect(chipModel(sampleSync({ status: "drifting" }))).toEqual({ text: "Adjusting", tone: "text-sync-drift", shape: "half", lost: false });
    expect(chipModel(sampleSync({ status: "correcting" }))).toEqual({ text: "Adjusting", tone: "text-sync-drift", shape: "half", lost: false });
    expect(chipModel(sampleSync({ status: "lost" }))).toEqual({ text: "Sync lost", tone: "text-sync-lost", shape: "ring", lost: true });
  });
  it("never uses the word perfect", () => {
    for (const status of ["resolving", "priming", "verifying", "starting", "locked", "drifting", "correcting", "lost"] as const) {
      expect(chipModel(sampleSync({ status }))!.text.toLowerCase()).not.toContain("perfect");
    }
  });
});

describe("active vs mirrored", () => {
  it("active covers every live status including lost; mirrored excludes lost/stopped/idle", () => {
    expect(isSyncActive(sampleSync({ status: "lost" }))).toBe(true);
    expect(isSyncMirrored(sampleSync({ status: "lost" }))).toBe(false);
    for (const status of ["resolving", "priming", "verifying", "starting", "locked", "drifting", "correcting"] as const) {
      expect(isSyncMirrored(sampleSync({ status }))).toBe(true);
    }
    expect(isSyncActive(sampleSync({ status: "stopped" }))).toBe(false);
    expect(isSyncActive(idleSync())).toBe(false);
  });
  it("syncSideIds lists master then follower while active; mirroredSideIds is empty once lost (dots do not claim both live)", () => {
    expect(syncSideIds(sampleSync())).toEqual(["heos:heos-1", "sonos:sonos-gK"]);
    expect(syncSideIds(sampleSync({ status: "lost" }))).toEqual(["heos:heos-1", "sonos:sonos-gK"]);
    expect(mirroredSideIds(sampleSync())).toEqual(["heos:heos-1", "sonos:sonos-gK"]);
    expect(mirroredSideIds(sampleSync({ status: "lost" }))).toEqual([]);
    expect(syncSideIds(sampleSync({ status: "stopped" }))).toEqual([]);
  });
  it("labels: full names joined with 'and' (grouped sides already use '+'); short form counts rooms; unknown side → 'another room'", () => {
    expect(syncLabel(sampleSync(), sampleState().sides)).toBe("Syncing Living Room Amp and Kitchen + 1");
    expect(syncLabel(sampleSync({ follower_side: "sonos:gone" }), sampleState().sides)).toBe("Syncing Living Room Amp and another room");
    expect(syncLabel(idleSync(), sampleState().sides)).toBeNull();
    expect(syncLabelShort(sampleSync())).toBe("Syncing 2 rooms");
    expect(syncLabelShort(sampleSync({ status: "stopped" }))).toBeNull();
  });
  it("transportTargetFor routes synced sides to the master only while mirrored; lost leaves the target unchanged", () => {
    const sync = sampleSync();
    expect(transportTargetFor("sonos:sonos-gK", sync)).toBe("heos:heos-1");
    expect(transportTargetFor("heos:heos-1", sync)).toBe("heos:heos-1");
    expect(transportTargetFor("heos:heos-9", sync)).toBe("heos:heos-9");
    expect(transportTargetFor("sonos:sonos-gK", sampleSync({ status: "lost" }))).toBe("sonos:sonos-gK");
    expect(transportTargetFor("sonos:sonos-gK", sampleSync({ status: "stopped" }))).toBe("sonos:sonos-gK");
    expect(transportTargetFor("sonos:sonos-gK", idleSync())).toBe("sonos:sonos-gK");
  });
});

describe("syncOffer (Now Playing secondary action)", () => {
  const state = sampleState();
  const online = () => true;
  it("offers the other vendor's side when the active side has a canonical Tidal content_ref, either vendor", () => {
    expect(syncOffer(state.sides["heos:heos-1"]!, tidalTrack, state.sides, online, idleSync())).toEqual({ heos: "heos:heos-1", sonos: "sonos:sonos-gK", partnerName: "Kitchen + 1" });
    expect(syncOffer(state.sides["sonos:sonos-gK"]!, tidalTrack, state.sides, online, idleSync())).toEqual({ heos: "heos:heos-1", sonos: "sonos:sonos-gK", partnerName: "Living Room Amp" });
  });
  it("no offer with content_ref null, non-Tidal content, an offline partner, or during a session", () => {
    expect(syncOffer(state.sides["heos:heos-1"]!, null, state.sides, online, idleSync())).toBeNull();
    expect(syncOffer(state.sides["heos:heos-1"]!, { service: "pandora", kind: "track", id: "x" }, state.sides, online, idleSync())).toBeNull();
    expect(syncOffer(state.sides["heos:heos-1"]!, tidalTrack, state.sides, () => false, idleSync())).toBeNull();
    expect(syncOffer(state.sides["heos:heos-1"]!, tidalTrack, state.sides, online, sampleSync())).toBeNull();
    expect(syncOffer(null, tidalTrack, state.sides, online, idleSync())).toBeNull();
  });
  it("is suppressed right after a stop for the rooms of the session that just ended", () => {
    const stopped = sampleSync({ status: "stopped" });
    expect(offerSuppressed(stopped, "heos:heos-1")).toBe(true);
    expect(offerSuppressed(stopped, "sonos:sonos-gK")).toBe(true);
    expect(offerSuppressed(stopped, "heos:heos-9")).toBe(false);
    expect(offerSuppressed(idleSync(), "heos:heos-1")).toBe(false);
    expect(syncOffer(state.sides["heos:heos-1"]!, tidalTrack, state.sides, online, stopped)).toBeNull();
    const other = sampleSync({ status: "stopped", master_side: "heos:heos-9", follower_side: "sonos:sonos-Z" });
    expect(syncOffer(state.sides["heos:heos-1"]!, tidalTrack, state.sides, online, other)).not.toBeNull();
  });
  it("partner rule: the vendor's playing side, else most recently used, else first online by name", () => {
    const sides = {
      ...state.sides,
      "sonos:sonos-gK": { ...state.sides["sonos:sonos-gK"]!, play_state: "stop" as const },
      "sonos:sonos-Z": side("sonos:sonos-Z", "Attic", "sonos", ["sonos-Z"]),
      "sonos:sonos-Y": side("sonos:sonos-Y", "Yard", "sonos", ["sonos-Y"], { play_state: "play" }),
    };
    const active = state.sides["heos:heos-1"]!;
    expect(syncOffer(active, tidalTrack, sides, online, idleSync())!.partnerName).toBe("Yard");
    const noPlaying = { ...sides, "sonos:sonos-Y": { ...sides["sonos:sonos-Y"], play_state: "stop" as const } };
    expect(syncOffer(active, tidalTrack, noPlaying, online, idleSync(), ["sonos:sonos-gK"])!.partnerName).toBe("Kitchen + 1");
    expect(syncOffer(active, tidalTrack, noPlaying, online, idleSync(), [])!.partnerName).toBe("Attic");
    expect(syncOffer(active, tidalTrack, noPlaying, (s) => s.id !== "sonos:sonos-Z", idleSync(), [])!.partnerName).toBe("Kitchen + 1");
  });
});

describe("offerContent (what the offer plays)", () => {
  const art = { url: null, accent: null, accent_is_safe: false };
  const album: HistoryItem = { content_ref: { service: "tidal", kind: "album", id: "a-1" }, title: "Kind of Blue", subtitle: "Miles Davis", art, last_targets: ["heos:heos-1"], last_played_at: "2026-09-07T00:00:00Z", play_count: 2, availability: null };
  const playlist: HistoryItem = { ...album, content_ref: { service: "tidal", kind: "playlist", id: "p-1" }, title: "Mix", last_targets: ["sonos:sonos-gK"] };
  const detail = (id: string, trackIds: string[]): Detail => ({
    item: { content_ref: { service: "tidal", kind: "album", id }, title: "x", subtitle: null, art } as Detail["item"],
    tracks: trackIds.map((tid, i) => ({ content_ref: { service: "tidal", kind: "track", id: tid }, title: `T${i}`, subtitle: null, art, index: i, artist: null, album: null })) as Detail["tracks"],
    truncated: false,
  });
  const key = (r: { service: string; kind: string; id: string }) => `${r.service}:${r.kind}:${r.id}`;
  it("prefers the room's most recent container whose cached detail holds the current track, starting at that track", () => {
    expect(offerContent(tidalTrack, "heos:heos-1", [album, playlist], { "tidal:album:a-1": detail("a-1", ["0", "1", "2"]) }, key)).toEqual({
      content_ref: album.content_ref,
      start_index: 1,
      title: "Kind of Blue",
      subtitle: "Miles Davis",
    });
  });
  it("falls back to the bare track when the container was played on another room, has no cached detail, or lacks the track", () => {
    const bare = { content_ref: { service: "tidal", kind: "track", id: "1" } };
    expect(offerContent(tidalTrack, "sonos:sonos-gK", [album], { "tidal:album:a-1": detail("a-1", ["0", "1"]) }, key)).toEqual(bare);
    expect(offerContent(tidalTrack, "heos:heos-1", [album], {}, key)).toEqual(bare);
    expect(offerContent(tidalTrack, "heos:heos-1", [album], { "tidal:album:a-1": detail("a-1", ["7"]) }, key)).toEqual(bare);
    expect(offerContent(tidalTrack, "heos:heos-1", null, {}, key)).toEqual(bare);
  });
});
