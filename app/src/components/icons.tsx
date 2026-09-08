/**
 * Icon set (design system §5): Lucide for chrome, filled glyphs for transport, custom ±15s
 * circular-arrow-with-number, and amp/speaker glyphs for HEOS zones vs Sonos players.
 */
import type { SVGProps } from "react";

type P = SVGProps<SVGSVGElement> & { size?: number };

function base({ size = 24, ...rest }: P, children: React.ReactNode, filled = false) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill={filled ? "currentColor" : "none"}
      stroke={filled ? "none" : "currentColor"}
      strokeWidth={1.75}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      focusable="false"
      {...rest}
    >
      {children}
    </svg>
  );
}

export const PlayIcon = (p: P) => base(p, <path d="M7 4.5v15a1 1 0 0 0 1.53.85l12-7.5a1 1 0 0 0 0-1.7l-12-7.5A1 1 0 0 0 7 4.5Z" />, true);
export const PauseIcon = (p: P) =>
  base(p, <><rect x="5" y="4" width="5" height="16" rx="1.2" /><rect x="14" y="4" width="5" height="16" rx="1.2" /></>, true);
export const NextIcon = (p: P) =>
  base(p, <><path d="M5 5.5v13a1 1 0 0 0 1.55.83L16 13.2V18a1 1 0 0 0 1 1h1a1 1 0 0 0 1-1V6a1 1 0 0 0-1-1h-1a1 1 0 0 0-1 1v4.8L6.55 4.67A1 1 0 0 0 5 5.5Z" /></>, true);
export const PrevIcon = (p: P) =>
  base(p, <><path d="M19 5.5v13a1 1 0 0 1-1.55.83L8 13.2V18a1 1 0 0 1-1 1H6a1 1 0 0 1-1-1V6a1 1 0 0 1 1-1h1a1 1 0 0 1 1 1v4.8l9.45-6.13A1 1 0 0 1 19 5.5Z" /></>, true);
export const StopIcon = (p: P) => base(p, <rect x="5" y="5" width="14" height="14" rx="2" />, true);

/** ±15s: circular arrow with the number set inside the icon (§5). */
function skipGlyph(p: P, forward: boolean) {
  return base(
    p,
    <>
      <path d="M4.5 12a7.5 7.5 0 1 0 2.2-5.3" transform={forward ? "scale(-1,1) translate(-24,0)" : undefined} />
      {forward ? <path d="M17.3 3.2v4h-4" /> : <path d="M6.7 3.2v4h4" />}
      <text
        x="12"
        y="15.2"
        textAnchor="middle"
        fontSize="7.5"
        fontWeight="700"
        fill="currentColor"
        stroke="none"
        fontFamily="inherit"
      >
        15
      </text>
    </>,
  );
}
export const SkipBack15Icon = (p: P) => skipGlyph(p, false);
export const SkipForward15Icon = (p: P) => skipGlyph(p, true);

/** Amplifier glyph: HEOS zones. Users think in hardware (§5). */
export const AmpIcon = (p: P) =>
  base(
    p,
    <>
      <rect x="3" y="7" width="18" height="10" rx="2" />
      <circle cx="16.5" cy="12" r="2" />
      <path d="M6.5 10.5h4M6.5 13.5h4" />
    </>,
  );

/** Speaker glyph: Sonos players. */
export const SpeakerIcon = (p: P) =>
  base(
    p,
    <>
      <rect x="6" y="3" width="12" height="18" rx="2.5" />
      <circle cx="12" cy="14" r="3.2" />
      <circle cx="12" cy="7.2" r="1" />
    </>,
  );

export const VolumeIcon = (p: P) =>
  base(p, <><path d="M11 5 6 9H3v6h3l5 4V5Z" /><path d="M15.5 8.5a5 5 0 0 1 0 7" /><path d="M18.5 5.5a9 9 0 0 1 0 13" /></>);
export const MuteIcon = (p: P) => base(p, <><path d="M11 5 6 9H3v6h3l5 4V5Z" /><path d="m22 9-6 6M16 9l6 6" /></>);
export const ChevronDownIcon = (p: P) => base(p, <path d="m6 9 6 6 6-6" />);
export const GearIcon = (p: P) =>
  base(
    p,
    <>
      <circle cx="12" cy="12" r="3" />
      <path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1Z" />
    </>,
  );
export const PowerIcon = (p: P) => base(p, <><path d="M12 3v9" /><path d="M18.4 6.6a9 9 0 1 1-12.8 0" /></>);
export const CheckIcon = (p: P) => base(p, <path d="m5 12 5 5L20 7" />);
export const LayersIcon = (p: P) =>
  base(p, <><path d="m12 3 9 5-9 5-9-5 9-5Z" /><path d="m3 13 9 5 9-5" /></>);

/** Chrome (Lucide-weight strokes): navigation chevrons, external link, refresh. */
export const ChevronRightIcon = (p: P) => base(p, <path d="m9 6 6 6-6 6" />);
export const ChevronLeftIcon = (p: P) => base(p, <path d="m15 6-6 6 6 6" />);
export const ExternalLinkIcon = (p: P) =>
  base(p, <><path d="M15 3h6v6" /><path d="M10 14 21 3" /><path d="M21 14v5a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5" /></>);
export const RefreshIcon = (p: P) =>
  base(p, <><path d="M21 12a9 9 0 1 1-2.64-6.36" /><path d="M21 3v6h-6" /></>);
export const HubIcon = (p: P) =>
  base(p, <><rect x="3" y="4" width="18" height="12" rx="2" /><path d="M8 20h8" /><path d="M12 16v4" /></>);
