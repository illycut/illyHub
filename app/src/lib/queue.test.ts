import { QUEUE_EMPTY, QUEUE_STATION, currentQueueIndex, isRadioSource, queueRowLabel, queueView, truncatedNote } from "./queue";
import { sampleState, side, stationNowPlaying } from "@/test/fixtures";
import type { Queue } from "./hub/library";

const art = { url: null, accent: null, accent_is_safe: false };
const q = (over: Partial<Queue> = {}): Queue => ({
  items: [
    { index: 0, title: "So What", artist: "Miles Davis", album: "Kind of Blue", duration_ms: 562_000, art, track_id: "t0", content_ref: { service: "tidal", kind: "track", id: "0" } },
    { index: 1, title: "Blue in Green", artist: "Miles Davis", album: "Kind of Blue", duration_ms: 337_000, art, track_id: "t1", content_ref: { service: "tidal", kind: "track", id: "1" } },
    { index: 2, title: "All Blues", artist: "Miles Davis", album: "Kind of Blue", duration_ms: 693_000, art, track_id: "t2", content_ref: null },
  ],
  current_index: null,
  source: "tidal",
  truncated: false,
  ...over,
});

describe("queue view logic", () => {
  const np = sampleState().now_playing["heos:heos-1"]!;
  const s = side("heos:heos-1", "Living Room Amp", "heos", ["heos-1"]);

  it("radio sources have no queue", () => {
    expect(isRadioSource(stationNowPlaying())).toBe(true);
    expect(isRadioSource(np)).toBe(false);
    expect(queueView(s, stationNowPlaying(), q(), false)).toEqual({ kind: "station", text: QUEUE_STATION });
  });

  it("current entry: the hub's index when it names one, else by track id, else by title + artist, else null", () => {
    expect(currentQueueIndex(q({ current_index: 2 }), np)).toBe(2);
    // the hub's index must exist in the list, otherwise fall through to matching
    expect(currentQueueIndex(q({ current_index: 9 }), np)).toBe(1);
    expect(currentQueueIndex(q(), { ...np, track_id: "sonos:x:1" })).toBe(1);
    expect(currentQueueIndex(q(), { ...np, track_id: null, title: "All Blues", artist: "Miles Davis" })).toBe(2);
    expect(currentQueueIndex(q(), { ...np, track_id: "nope", title: "Other" })).toBeNull();
    expect(currentQueueIndex(null, np)).toBeNull();
  });

  it("truncation note names the count shown; empty and loading states; row label", () => {
    expect(truncatedNote(q({ truncated: true }))).toBe("Showing the first 3 tracks.");
    expect(truncatedNote(q())).toBeNull();
    expect(queueView(s, np, undefined, true)).toEqual({ kind: "loading" });
    expect(queueView(s, np, undefined, false)).toEqual({ kind: "empty", text: QUEUE_EMPTY });
    expect(queueView(s, np, q({ items: [] }), false)).toEqual({ kind: "empty", text: QUEUE_EMPTY });
    expect(queueView(null, np, q(), false)).toEqual({ kind: "empty", text: QUEUE_EMPTY });
    const list = queueView(s, np, q({ truncated: true }), false);
    expect(list.kind).toBe("list");
    if (list.kind === "list") {
      expect(list.current).toBe(1);
      expect(list.note).toBe("Showing the first 3 tracks.");
    }
    expect(queueRowLabel(q().items[0]!)).toBe("Play from So What, Miles Davis");
    expect(queueRowLabel({ ...q().items[0]!, artist: null })).toBe("Play from So What");
  });
});
