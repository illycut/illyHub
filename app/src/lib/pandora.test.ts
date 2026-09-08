import { readFileSync } from "node:fs";
import path from "node:path";
import {
  PANDORA_CONCURRENT_NOTE,
  pandoraConcurrentNote,
  pandoraPlayingSides,
  pandoraSetupNote,
  stationSubtitle,
  stationsNote,
  stationsSectionModel,
  unavailableCopy,
  unlinkedVendors,
  warningToast,
} from "./pandora";
import { STATIONS_CANT_SYNC, syncEligibility } from "./sync";
import { sampleState, side, stationNowPlaying } from "@/test/fixtures";
import type { LibraryItem, Section } from "./hub/library";

const art = { url: null, accent: null, accent_is_safe: false };
const station = (id: string, title: string, availability = { heos: true, sonos: true }): LibraryItem => ({
  content_ref: { service: "pandora", kind: "station", id },
  title,
  subtitle: "Pandora station",
  art,
  duration_ms: null,
  track_count: null,
  availability,
});
const section = (items: LibraryItem[], over: Partial<Section<LibraryItem>> = {}): Section<LibraryItem> => ({ items, needs_link: [], error: null, linked: { heos: true, sonos: true }, ...over });
const rooms = { heos: ["Living Room Amp", "Den"], sonos: ["Kitchen + 1"] };

describe("stationsSectionModel", () => {
  it("sorts by title with base sensitivity (case and accents ignored), visible with items, no note when both vendors are linked", () => {
    const m = stationsSectionModel(section([station("b", "zen garden"), station("a", "Acoustic Morning"), station("e", "Émile's Jazz"), station("c", "Jazz Lounge")]), rooms);
    expect(m.visible).toBe(true);
    expect(m.items.map((i) => i.title)).toEqual(["Acoustic Morning", "Émile's Jazz", "Jazz Lounge", "zen garden"]);
    expect(m.missing).toEqual([]);
    expect(m.note).toBeNull();
  });

  it("is hidden while empty (also with needs_link) and has no note without items", () => {
    expect(stationsSectionModel(null).visible).toBe(false);
    expect(stationsSectionModel(section([])).visible).toBe(false);
    const unlinked = stationsSectionModel(section([], { needs_link: ["pandora"], linked: { heos: false, sonos: false } }), rooms);
    expect(unlinked.visible).toBe(false);
    expect(unlinked.missing).toEqual(["heos", "sonos"]);
    expect(unlinked.note).toBeNull();
  });

  it("names the rooms and the vendor app for a vendor the hub reports as not linked; null (adapter absent) says nothing", () => {
    const heosOff = stationsSectionModel(section([station("a", "A", { heos: false, sonos: true })], { linked: { heos: false, sonos: true } }), rooms);
    expect(heosOff.missing).toEqual(["heos"]);
    expect(heosOff.note).toBe("Living Room Amp and Den can't play these until Pandora is added in the HEOS app.");
    const sonosOff = stationsSectionModel(section([station("a", "A")], { linked: { heos: true, sonos: false } }), rooms);
    expect(sonosOff.note).toBe("Kitchen + 1 can't play these until Pandora is added in the Sonos app.");
    // no rooms known yet: the vendor's rooms, generically
    expect(stationsNote(["heos"], {})).toBe("HEOS rooms can't play these until Pandora is added in the HEOS app.");
    const unknown = stationsSectionModel(section([station("a", "A", { heos: false, sonos: true })], { linked: { heos: null, sonos: true } }), rooms);
    expect(unknown.missing).toEqual([]);
    expect(unknown.note).toBeNull();
    // availability alone never drives the note
    expect(stationsSectionModel(section([station("a", "A", { heos: false, sonos: true })], { linked: null }), rooms).note).toBeNull();
  });

  it("is visible with a hub-side section error even without items, and carries no note", () => {
    const m = stationsSectionModel(section([], { error: "Pandora stations didn't load from HEOS.", linked: { heos: false, sonos: true } }), rooms);
    expect(m.visible).toBe(true);
    expect(m.items).toEqual([]);
    expect(m.note).toBeNull();
  });
});

describe("copy", () => {
  it("setup note, unlinked vendors, and the row reason: the fix for an unlinked vendor, the fact for a linked one", () => {
    expect(pandoraSetupNote([])).toBeNull();
    expect(pandoraSetupNote(["heos", "sonos"])).toBe("Pandora is set up in the HEOS app and the Sonos app.");
    expect(unlinkedVendors({ heos: false, sonos: true })).toEqual(["heos"]);
    expect(unlinkedVendors({ heos: null, sonos: false })).toEqual(["sonos"]);
    expect(unlinkedVendors(null)).toEqual([]);
    expect(unavailableCopy("pandora", "heos", { unlinked: ["heos"] })).toBe("Pandora is set up in the HEOS app.");
    expect(unavailableCopy("pandora", "sonos", { unlinked: ["sonos"] })).toBe("Pandora is set up in the Sonos app.");
    expect(unavailableCopy("pandora", "heos", { unlinked: [] })).toBe("Not in the HEOS Pandora account.");
    expect(unavailableCopy("pandora", "sonos")).toBe("Not in the Sonos Pandora account.");
    // the hub's reason wins over the link-state fallback
    expect(unavailableCopy("pandora", "sonos", { availability: { heos: true, sonos: false, reasons: { heos: null, sonos: "auth_fault" } } })).toBe("Sonos can't reach Pandora right now; sign in again in the Sonos app.");
    // one sentence form for every service: no short "Not available on X"
    expect(unavailableCopy("tidal", "heos")).toBe("Tidal is set up in the HEOS app.");
    expect(unavailableCopy("ytmusic", "heos")).toBe("YouTube Music isn't available on HEOS.");
    expect(unavailableCopy(undefined, "sonos")).toBe("Tidal is set up in the Sonos app.");
  });

  it("station subtitle comes from availability", () => {
    expect(stationSubtitle({ heos: true, sonos: true })).toBeNull();
    expect(stationSubtitle({ heos: true, sonos: false })).toBe("HEOS rooms only");
    expect(stationSubtitle({ heos: false, sonos: true })).toBe("Sonos rooms only");
    expect(stationSubtitle({ heos: false, sonos: false })).toBeNull();
    expect(stationSubtitle(null)).toBeNull();
  });

  it("warning toast names the room the hub says may pause, else the hub sentence", () => {
    const sides = { "heos:heos-1": { name: "Living Room Amp" } };
    expect(warningToast({ code: "pandora_concurrent", message: PANDORA_CONCURRENT_NOTE, target: "heos:heos-1" }, sides)).toBe("Living Room Amp may pause: Pandora usually allows one stream per account.");
    expect(warningToast({ code: "pandora_concurrent", message: PANDORA_CONCURRENT_NOTE, target: null }, sides)).toBe(PANDORA_CONCURRENT_NOTE);
    expect(warningToast({ code: "pandora_concurrent", message: PANDORA_CONCURRENT_NOTE, target: "gone" }, sides)).toBe(PANDORA_CONCURRENT_NOTE);
    expect(warningToast({ code: "other", message: "Hub says so.", target: "heos:heos-1" }, sides)).toBe("Hub says so.");
  });

  it("the single-stream note equals the hub's PANDORA_CONCURRENT_MSG as documented in docs/api.md", () => {
    const api = readFileSync(path.resolve(__dirname, "../../../docs/api.md"), "utf8");
    expect(api).toContain(`"code": "pandora_concurrent"`);
    expect(api).toContain(PANDORA_CONCURRENT_NOTE);
  });

  it("stations never sync: eligibility reports the station reason when both vendors are selected", () => {
    const sides = sampleState().sides;
    expect(syncEligibility(["heos:heos-1", "sonos:sonos-gK"], sides, { service: "pandora", kind: "station", id: "x" })).toEqual({ mode: "play", syncReason: STATIONS_CANT_SYNC });
    expect(syncEligibility(["heos:heos-1"], sides, { service: "pandora", kind: "station", id: "x" })).toEqual({ mode: "play", syncReason: null });
  });
});

describe("pandoraConcurrentNote", () => {
  // sampleState: the Sonos group side holds a Pandora station but is stopped; the HEOS side plays Tidal.
  const state = sampleState();
  const playingSonos = { ...state, sides: { ...state.sides, "sonos:sonos-gK": { ...state.sides["sonos:sonos-gK"]!, play_state: "play" as const } } };

  it("only applies to Pandora content, and never with an empty selection", () => {
    expect(pandoraConcurrentNote("tidal", ["heos:heos-1", "sonos:sonos-gK"], playingSonos)).toBeNull();
    expect(pandoraConcurrentNote(undefined, ["heos:heos-1", "sonos:sonos-gK"], playingSonos)).toBeNull();
    expect(pandoraConcurrentNote("pandora", [], playingSonos)).toBeNull();
  });

  it("two or more selected rooms always warn", () => {
    expect(pandoraConcurrentNote("pandora", ["heos:heos-1", "sonos:sonos-gK"], state)).toBe(PANDORA_CONCURRENT_NOTE);
  });

  it("one room warns only when another room is already playing Pandora, whichever vendor it is", () => {
    expect(pandoraPlayingSides(state)).toEqual([]);
    expect(pandoraConcurrentNote("pandora", ["heos:heos-1"], state)).toBeNull();
    expect(pandoraPlayingSides(playingSonos)).toEqual(["sonos:sonos-gK"]);
    expect(pandoraConcurrentNote("pandora", ["heos:heos-1"], playingSonos)).toBe(PANDORA_CONCURRENT_NOTE);
    // the room already playing Pandora is the one selected: it just switches station, no warning
    expect(pandoraConcurrentNote("pandora", ["sonos:sonos-gK"], playingSonos)).toBeNull();
    // same vendor: a second HEOS room already streaming Pandora warns too
    const den = side("heos:heos-2", "Den", "heos", ["heos-2"], { play_state: "play" });
    const sameVendor = { ...state, sides: { ...state.sides, "heos:heos-2": den }, now_playing: { ...state.now_playing, "heos:heos-2": stationNowPlaying() } };
    expect(pandoraPlayingSides(sameVendor)).toEqual(["heos:heos-2"]);
    expect(pandoraConcurrentNote("pandora", ["heos:heos-1"], sameVendor)).toBe(PANDORA_CONCURRENT_NOTE);
  });
});
