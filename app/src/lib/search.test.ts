import { SEARCH_MIN_CHARS, isEmptyResults, searchErrorCaption, searchQuery } from "./search";
import { asSearchResults, type LibraryItem, type SearchResults } from "./hub/library";

const art = { url: null, accent: null, accent_is_safe: false };
const item = (id: string, kind: "album" | "playlist" | "track" = "album"): LibraryItem => ({ content_ref: { service: "tidal", kind, id }, title: `T${id}`, subtitle: null, art, duration_ms: null, track_count: null, availability: { heos: true, sonos: true } });

describe("search logic", () => {
  it("queries under two characters are not sent; whitespace is collapsed", () => {
    expect(SEARCH_MIN_CHARS).toBe(2);
    expect(searchQuery("")).toBeNull();
    expect(searchQuery(" a ")).toBeNull();
    expect(searchQuery("  kind   of  blue ")).toBe("kind of blue");
  });

  it("normalises the hub payload: groups, tracks with indices, and per-service errors", () => {
    const r = asSearchResults({ albums: [item("a1")], playlists: [], tracks: [{ ...item("t1", "track"), artist: "Miles" }], errors: { tidal: "timed out", bogus: "x", ytmusic: 3 } });
    expect(r.albums).toHaveLength(1);
    expect(r.stations).toEqual([]);
    expect(r.tracks[0]!.artist).toBe("Miles");
    expect(r.tracks[0]!.index).toBe(0);
    expect(r.errors).toEqual({ tidal: "timed out" });
    expect(isEmptyResults(r)).toBe(false);
    expect(isEmptyResults(asSearchResults({}))).toBe(true);
    expect(isEmptyResults(null)).toBe(true);
  });

  it("error caption: names the failed service and, when something else answered, says whose results show", () => {
    const some: SearchResults = { albums: [item("a1")], playlists: [], tracks: [], stations: [], services: ["tidal", "ytmusic", "pandora"], errors: { tidal: "timed out" }, partial: true, query: "x" };
    expect(searchErrorCaption(some, ["tidal", "ytmusic"])).toBe("Tidal didn't answer; showing YouTube Music results.");
    const none: SearchResults = { ...some, albums: [] };
    expect(searchErrorCaption(none, ["tidal", "ytmusic"])).toBe("Tidal didn't answer.");
    const both: SearchResults = { ...some, errors: { tidal: "x", ytmusic: "y" }, partial: true };
    expect(searchErrorCaption(both, ["tidal", "ytmusic"])).toBe("Tidal and YouTube Music didn't answer.");
    expect(searchErrorCaption({ ...some, errors: {} })).toBeNull();
    expect(searchErrorCaption(null)).toBeNull();
  });
});
