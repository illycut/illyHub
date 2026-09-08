import type { Config } from "tailwindcss";

/**
 * Every value maps to a CSS custom property declared in src/styles/tokens.css so the design
 * system lives in one file (design system §11). Colors use rgb triplets so alpha modifiers
 * (`bg-base/60`) work. The default spacing scale is EXTENDED, not replaced, so utilities like
 * h-7 keep working; token-named sizes sit alongside.
 */
const rgb = (name: string) => `rgb(var(--${name}-rgb) / <alpha-value>)`;

const config: Config = {
  content: ["./src/**/*.{ts,tsx}"],
  theme: {
    colors: {
      transparent: "transparent",
      current: "currentColor",
      base: rgb("bg-base"),
      raised: rgb("bg-raised"),
      overlay: rgb("bg-overlay"),
      stroke: rgb("stroke"),
      primary: rgb("text-primary"),
      secondary: rgb("text-secondary"),
      tertiary: rgb("text-tertiary"),
      signal: rgb("signal"),
      "signal-dim": rgb("signal-dim"),
      "sync-locked": rgb("sync-locked"),
      "sync-drift": rgb("sync-drift"),
      "sync-lost": rgb("sync-lost"),
      error: rgb("error"),
      "badge-tidal": rgb("badge-tidal-bg"),
      "badge-tidal-fg": rgb("badge-tidal-fg"),
      "badge-ytmusic": rgb("badge-ytmusic-bg"),
      "badge-ytmusic-fg": rgb("badge-ytmusic-fg"),
      "badge-pandora": rgb("badge-pandora-bg"),
      "badge-pandora-fg": rgb("badge-pandora-fg"),
    },
    borderRadius: {
      none: "0",
      art: "var(--radius-art)",
      control: "var(--radius-control)",
      sheet: "var(--radius-sheet)",
      pill: "var(--radius-pill)",
    },
    fontFamily: {
      sans: ["var(--font-sans)"],
    },
    fontSize: {
      display: ["var(--type-display-size)", { lineHeight: "var(--type-display-line)", fontWeight: "700" }],
      "title-1": ["var(--type-title-1-size)", { lineHeight: "var(--type-title-1-line)", fontWeight: "700" }],
      "title-2": ["var(--type-title-2-size)", { lineHeight: "var(--type-title-2-line)", fontWeight: "600" }],
      body: ["var(--type-body-size)", { lineHeight: "var(--type-body-line)", fontWeight: "500" }],
      caption: ["var(--type-caption-size)", { lineHeight: "var(--type-caption-line)", fontWeight: "500" }],
      micro: ["var(--type-micro-size)", { lineHeight: "var(--type-micro-line)", fontWeight: "600" }],
    },
    fontWeight: { medium: "500", semibold: "600", bold: "700" },
    boxShadow: {
      none: "none",
      mini: "var(--shadow-mini-player)",
    },
    extend: {
      spacing: {
        half: "var(--space-half)",
        "thumb-art": "var(--size-thumb-art)",
        mini: "var(--size-mini-player)",
        target: "var(--size-target)",
        "target-lg": "var(--size-target-lg)",
        play: "var(--size-play)",
        row: "var(--size-row)",
        card: "var(--size-card)",
        "slider-track": "var(--size-slider-track)",
        "slider-track-active": "var(--size-slider-track-active)",
        "slider-thumb": "var(--size-slider-thumb)",
        "scrub-thumb": "var(--size-scrub-thumb)",
        "scrub-thumb-active": "var(--size-scrub-thumb-active)",
        "sheet-handle": "var(--size-sheet-handle)",
        "gap-min": "var(--gap-target-min)",
      },
      screens: { tablet: "768px", "np-landscape": { raw: "(min-width: 700px) and (orientation: landscape)" } },
      transitionDuration: {
        press: "var(--motion-press)",
        fade: "var(--motion-fade)",
        scrub: "var(--motion-scrub)",
        expand: "var(--motion-expand)",
        pulse: "var(--motion-pulse)",
      },
      maxWidth: { transport: "var(--size-transport-row)", hero: "var(--size-hero-max)" },
    },
  },
  plugins: [],
};

export default config;
