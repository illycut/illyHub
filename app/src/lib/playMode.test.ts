import { PLAY_MODES_OFF_DURING_SYNC, nextRepeat, playModeOf, playModesHidden, repeatLabel, shuffleLabel } from "./playMode";
import { sampleState, side, stationNowPlaying } from "@/test/fixtures";
import type { Side } from "./hub/types";

describe("play modes", () => {
  it("repeat cycles off → all → one → off", () => {
    expect(nextRepeat("off")).toBe("all");
    expect(nextRepeat("all")).toBe("one");
    expect(nextRepeat("one")).toBe("off");
  });

  it("reads the generated side.play_mode and defaults to both off on a hub that predates it", () => {
    const plain = side("heos:heos-1", "Living Room Amp", "heos", ["heos-1"]);
    expect(playModeOf(plain)).toEqual({ shuffle: false, repeat: "off" });
    expect(playModeOf(null)).toEqual({ shuffle: false, repeat: "off" });
    const withMode: Side = { ...plain, play_mode: { shuffle: true, repeat: "one" } };
    expect(playModeOf(withMode)).toEqual({ shuffle: true, repeat: "one" });
    // garbage from an older hub never throws
    expect(playModeOf({ ...plain, play_mode: { shuffle: "yes", repeat: "sometimes" } as unknown as Side["play_mode"] })).toEqual({ shuffle: false, repeat: "off" });
  });

  it("accessible names carry the mode as text (U11); stations hide the toggles; the sync lock sentence is one string", () => {
    expect(shuffleLabel()).toBe("Shuffle");
    expect(repeatLabel("off")).toBe("Repeat");
    expect(repeatLabel("all")).toBe("Repeat all");
    expect(repeatLabel("one")).toBe("Repeat one");
    expect(playModesHidden(stationNowPlaying())).toBe(true);
    expect(playModesHidden(sampleState().now_playing["heos:heos-1"])).toBe(false);
    expect(playModesHidden(undefined)).toBe(false);
    expect(PLAY_MODES_OFF_DURING_SYNC.length).toBeGreaterThan(10);
    expect(PLAY_MODES_OFF_DURING_SYNC).not.toMatch(/perfect/i);
  });
});
