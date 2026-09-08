# Service brand assets

Design system §5.1 and ai-dev #53 require official brand-kit marks for Tidal, YouTube Music, and
Pandora, rendered inside the service chip. None could be fetched automatically on
September 7, 2026, so the app ships **simplified stand-in glyphs** (`ServiceBadge.tsx`) that
label the source without imitating the marks. Replace them with the official assets below and
keep this file as the record of source and terms.

| Service | Where the official mark lives | Result of automated fetch | Terms to check |
|---|---|---|---|
| Tidal | https://tidal.com/brand (brand guidelines and downloadable logos) | 403 (bot protection); must be downloaded in a browser | "Available on TIDAL" usage, minimum size, clear space, do-not-alter colors |
| YouTube Music | https://www.youtube.com/howyoutubeworks/resources/brand-resources/ (YouTube Brand Resources; the logo pack is a gated download) | Page loads; no direct asset URL exposed | YouTube logo guidelines: the icon may be used to indicate availability; red `#FF0000` icon on light, white on dark; never recolor |
| Pandora | https://www.pandoraforbrands.com/ and the SiriusXM Media press kit | Page loads; assets are behind a request form | Pandora brand guidelines: "P" mark in Pandora blue; no recoloring |

## How to install the official marks

1. Download the SVG mark (icon only, no wordmark) for each service into this folder as
   `tidal.svg`, `ytmusic.svg`, `pandora.svg`.
2. In `src/components/ServiceBadge.tsx`, set `OFFICIAL = true`; the chip switches from the
   stand-in glyph to `<img src="/brands/<service>.svg">` at 68–72% of the chip size.
3. Where a kit requires the original mark color (YouTube red, Pandora blue), the chip background
   stays the muted service color from design system §2.4 and the mark keeps its kit color.
4. Record the download date and the kit version here.

Logos identify the source of content only. They are never buttons and never deep-link to the
service's own app (design system §5.1).
