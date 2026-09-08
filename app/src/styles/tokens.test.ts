import { readFileSync } from "node:fs";
import path from "node:path";

const css = readFileSync(path.join(__dirname, "tokens.css"), "utf8");

const REQUIRED: [string, string][] = [
  ["--bg-base", "#101014"],
  ["--bg-raised", "#1a1a20"],
  ["--bg-overlay", "#24242c"],
  ["--stroke", "#2e2e38"],
  ["--text-primary", "#f2f2f5"],
  ["--text-secondary", "#a6a6b3"],
  ["--text-tertiary", "#6e6e7a"],
  ["--signal", "#f0a63c"],
  ["--signal-dim", "#8a6224"],
  ["--sync-locked", "#4cc38a"],
  ["--sync-drift", "#f0a63c"],
  ["--sync-lost", "#e5484d"],
  ["--error", "#e5484d"],
  ["--badge-tidal-bg", "#0e2a33"],
  ["--badge-tidal-fg", "#7fd4e8"],
  ["--badge-ytmusic-bg", "#331114"],
  ["--badge-ytmusic-fg", "#f08a8f"],
  ["--badge-pandora-bg", "#131a33"],
  ["--badge-pandora-fg", "#8fa8f0"],
  ["--type-display-size", "32px"],
  ["--type-title-1-size", "24px"],
  ["--type-title-2-size", "18px"],
  ["--type-body-size", "15px"],
  ["--type-caption-size", "13px"],
  ["--type-micro-size", "11px"],
  ["--radius-art", "8px"],
  ["--radius-control", "12px"],
  ["--radius-sheet", "20px"],
  ["--radius-pill", "999px"],
  ["--size-target", "48px"],
  ["--size-target-lg", "56px"],
  ["--size-play", "64px"],
  ["--size-mini-player", "64px"],
  ["--size-transport-row", "372px"],
  ["--motion-expand", "320ms"],
];

describe("design tokens", () => {
  it.each(REQUIRED)("%s = %s", (name, value) => {
    const re = new RegExp(`${name}:\\s*${value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")};`, "i");
    expect(css).toMatch(re);
  });
  it("declares the 4px spacing scale", () => {
    for (const [k, v] of [[1, 4], [2, 8], [3, 12], [4, 16], [6, 24], [8, 32], [12, 48]]) {
      expect(css).toMatch(new RegExp(`--space-${k}:\\s*${v}px;`));
    }
  });
  it("uses General Sans with the documented fallback stack", () => {
    expect(css).toMatch(/--font-sans: "General Sans", -apple-system, "Segoe UI", sans-serif;/);
  });
});
