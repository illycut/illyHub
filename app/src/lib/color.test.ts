import { contrastRatio, hexToRgb, relativeLuminance, scrubFill } from "./color";

describe("color", () => {
  it("parses hex and computes luminance/contrast", () => {
    expect(hexToRgb("#ffffff")).toEqual([255, 255, 255]);
    expect(hexToRgb("101014")).toEqual([16, 16, 20]);
    expect(hexToRgb("nope")).toBeNull();
    expect(relativeLuminance([255, 255, 255])).toBeCloseTo(1);
    expect(contrastRatio("#f2f2f5", "#101014")).toBeGreaterThan(13);
    expect(contrastRatio("bad", "#000000")).toBe(1);
  });
  it("scrubber fill: accent only when safe and >= 3:1 against the track, else text-primary", () => {
    expect(scrubFill("#f0a63c", true)).toBe("#f0a63c"); // amber vs stroke ≈ 7:1
    expect(scrubFill("#3a5a7a", true)).toBe("var(--text-primary)"); // dark blue vs stroke < 3:1
    expect(scrubFill("#f0a63c", false)).toBe("var(--text-primary)");
    expect(scrubFill(null, true)).toBe("var(--text-primary)");
  });
});
