import { msToPct, pxToMs } from "./scrub";
import { formatTime, roomsLabel } from "./format";
import { formatClock } from "./clock";

describe("scrub math", () => {
  it("maps pixels to ms with clamping at both ends", () => {
    expect(pxToMs(150, 100, 200, 60_000)).toBe(15_000);
    expect(pxToMs(50, 100, 200, 60_000)).toBe(0);
    expect(pxToMs(999, 100, 200, 60_000)).toBe(60_000);
    expect(pxToMs(150, 100, 0, 60_000)).toBe(0);
    expect(pxToMs(150, 100, 200, 0)).toBe(0);
  });
  it("maps ms to percent with clamping", () => {
    expect(msToPct(30_000, 60_000)).toBe(50);
    expect(msToPct(90_000, 60_000)).toBe(100);
    expect(msToPct(10, null)).toBe(0);
    expect(msToPct(-5, 100)).toBe(0);
  });
});

describe("format", () => {
  it("formats timecodes", () => {
    expect(formatTime(0)).toBe("0:00");
    expect(formatTime(65_000)).toBe("1:05");
    expect(formatTime(3_725_000)).toBe("1:02:05");
    expect(formatTime(null)).toBe("0:00");
    expect(formatTime(-1)).toBe("0:00");
  });
  it("pluralises rooms", () => {
    expect(roomsLabel(1)).toBe("1 room");
    expect(roomsLabel(5)).toBe("5 rooms");
  });
  it("formats a wall clock", () => {
    const d = new Date(2026, 8, 7, 9, 5, 3).getTime();
    expect(formatClock(d)).toBe("09:05:03");
  });
});
