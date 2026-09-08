import { interpolatePosition, shouldAnimate } from "./position";

const at = (ms: number) => new Date(ms).toISOString();

describe("interpolatePosition", () => {
  it("extrapolates forward from reported_at while playing", () => {
    expect(interpolatePosition({ position_ms: 10_000, reported_at: at(1000), confidence: 0.9 }, "play", 3500)).toBe(12_500);
  });
  it("does not advance when paused or stopped", () => {
    const p = { position_ms: 10_000, reported_at: at(1000), confidence: 1 };
    expect(interpolatePosition(p, "pause", 9000)).toBe(10_000);
    expect(interpolatePosition(p, "stop", 9000)).toBe(10_000);
  });
  it("clamps to duration and never goes negative; handles missing position and bad timestamps", () => {
    expect(interpolatePosition({ position_ms: 100_000, reported_at: at(0), confidence: 0.9 }, "play", 50_000, 120_000)).toBe(120_000);
    expect(interpolatePosition(null, "play", 1)).toBe(0);
    expect(interpolatePosition({ position_ms: 5, reported_at: "garbage", confidence: 0.9 }, "play", 1000)).toBe(5);
    expect(interpolatePosition({ position_ms: 5, reported_at: at(5000), confidence: 0.9 }, "play", 1000)).toBe(5); // clock skew, no rewind
  });
  it("animates only when the estimate is tight", () => {
    expect(shouldAnimate({ position_ms: 0, reported_at: at(0), confidence: 0.9 })).toBe(true);
    expect(shouldAnimate({ position_ms: 0, reported_at: at(0), confidence: 0.5 })).toBe(false);
    expect(shouldAnimate(null)).toBe(false);
  });
});
