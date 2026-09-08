# Design System
## Multi-Room Audio Control App

**Version:** 1.1 (see §13 changelog)
**Companion to:** multi-room-audio-PRD.md
**Date:** September 7, 2026

---

## 1. Design Personality

The reference points are Spotify and Sonos, borrowed for different reasons:

- **From Spotify:** playlist-forward home, artwork as the interface, dynamic color pulled from the active album, dense-but-scannable card grids.
- **From Sonos:** calm precision, generous negative space, room/zone awareness as a first-class concept, restraint in chrome.

The identity move that is ours alone: this app controls real hi-fi hardware, an amplifier with zones and two speaker ecosystems. The visual signature borrows from hardware, specifically the **amber signal glow** of power LEDs and VU meters. Amber is the app's accent for active state, sync status, and focus. Everything else stays neutral so artwork owns the color.

**Three rules that settle most decisions:**
1. Artwork carries color. Chrome is neutral and dark.
2. One accent (signal amber) means "live audio state." It is never decorative.
3. Every screen answers "what is playing, where" within one glance.

---

## 2. Color

### 2.1 Base palette (static tokens)

| Token | Hex | Use |
|-------|-----|-----|
| `bg-base` | `#101014` | App background |
| `bg-raised` | `#1A1A20` | Cards, sheets, mini-player |
| `bg-overlay` | `#24242C` | Pressed states, secondary surfaces |
| `stroke` | `#2E2E38` | Hairline borders, dividers |
| `text-primary` | `#F2F2F5` | Titles, primary labels |
| `text-secondary` | `#A6A6B3` | Artists, metadata, captions |
| `text-tertiary` | `#6E6E7A` | Timestamps, disabled |
| `signal` | `#F0A63C` | Active state, sync, focus, live indicators |
| `signal-dim` | `#8A6224` | Signal at rest (e.g., inactive zone dot) |

The base is a cool near-black with a faint violet cast rather than pure neutral. It flatters album art the way a dim listening room flatters a screen, and it keeps the amber accent from reading orange-on-brown.

### 2.2 Dynamic accent (art-derived)

The hub's art proxy computes a dominant color per artwork (server-side, cached with the image) and returns it as `art_accent` alongside the art URL. Rules:

- Used only on the Now Playing screen: blurred-art backdrop tint and scrubber fill.
- Clamped before use: lightness forced into 35 to 60 percent, saturation capped at 70 percent, so text contrast survives any album.
- Falls back to `bg-raised` when extraction fails or contrast check fails.
- Never used on home screen chrome. Home stays neutral so a grid of covers reads as a mosaic, not a fight.

### 2.3 Semantic colors

| Token | Hex | Use |
|-------|-----|-----|
| `sync-locked` | `#4CC38A` | Sync Play holding under drift threshold |
| `sync-drift` | `#F0A63C` | Drift detected, correction pending (reuses signal) |
| `sync-lost` | `#E5484D` | Sync abandoned or one system dropped |
| `error` | `#E5484D` | Failures, hub offline |

### 2.4 Service badges

Small identity chips on cards and now-playing. Muted versions of each brand's color so they label without shouting:

| Service | Chip | Text |
|---------|------|------|
| Tidal | `#0E2A33` | `#7FD4E8` |
| YouTube Music | `#331114` | `#F08A8F` |
| Pandora | `#131A33` | `#8FA8F0` |

---

## 3. Typography

**Family:** General Sans (Fontshare, free license), single family across the app. It has the geometric warmth of the faces Spotify and Sonos use without being a lookalike, a broad weight range, and tabular figures.

**Weights in use:** 500 (regular UI), 600 (titles, buttons), 700 (screen headers, hero).

**Numerals:** tabular lining figures (`font-variant-numeric: tabular-nums`) everywhere time or volume appears, so the scrubber timecode does not wobble as it counts.

### Type scale

| Token | Size / line | Weight | Use |
|-------|------------|--------|-----|
| `display` | 32 / 38 | 700 | Now Playing track title |
| `title-1` | 24 / 30 | 700 | Screen headers ("Your playlists") |
| `title-2` | 18 / 24 | 600 | Section headers, sheet titles |
| `body` | 15 / 22 | 500 | Card titles, list rows |
| `caption` | 13 / 18 | 500 | Artists, metadata, badges |
| `micro` | 11 / 14 | 600 | Timecodes, zone labels, chips |

Sentence case everywhere, including section headers and buttons. No letter-spaced caps labels.

Long titles truncate with ellipsis at one line on cards, two lines on Now Playing. Never marquee-scroll on the home screen; marquee is permitted only in the mini-player where width is scarce.

---

## 4. Spacing, Grid, Shape

**Base unit:** 4px. Spacing tokens: 4, 8, 12, 16, 24, 32, 48.

**Screen margins:** 16px phone, 24px tablet.

**Card grids:**
- Playlist/album grid: 2 columns phone, 4 tablet, 12px gutters
- Recents rail: horizontal scroll, cards at 148px (phone) / 180px (tablet), peek of next card always visible

**Radii (hierarchy, not one value everywhere):**

| Token | Value | Use |
|-------|-------|-----|
| `radius-art` | 8px | Artwork and art cards |
| `radius-control` | 12px | Buttons, chips, inputs |
| `radius-sheet` | 20px | Bottom sheets, expanded player |
| `radius-pill` | 999px | Badges, sync chip, zone dots |

**Elevation:** dark UIs elevate with lighter surface color, not shadow. `bg-base` → `bg-raised` → `bg-overlay` is the stack. One exception: the mini-player casts a soft upward shadow (`0 -8px 24px rgba(0,0,0,0.45)`) because it floats over scrolling content.

**Artwork sizes** (matches PRD art pipeline):
- Grid card: 320px source, displayed at 148 to 180px
- Recents rail: same
- Mini-player thumb: 96px source at 44px
- Now Playing hero: 1080px source, edge-to-edge with 8px radius

---

## 5. Iconography

- Single stroke-based set (Lucide), 1.75px stroke, 24px frame, 20px in dense rows.
- Transport icons are filled, not stroked: play, pause, prev, next read faster as solids.
- The ±15s skip icons use the circular-arrow-with-number treatment (like podcast apps). The number is set in the icon, not beside it.
- Zone/system icons: amplifier glyph for HEOS zones, speaker glyph for Sonos players. The distinction earns its place; users think in hardware.

### 5.1 Platform logos
Service identity uses each platform's logo mark, not text labels. Rules:
- **Production assets come from the official brand kits.** Tidal, YouTube (for YT Music), and Pandora each publish brand guidelines with downloadable marks and usage terms covering "available on" contexts. Ship those, not redrawn marks. Mockups may use simplified stand-ins.
- Logos render inside a circular chip: 22px on cards, 28px in settings rows, mark at roughly 68 to 72 percent of chip size.
- Chip background uses the muted service color (Section 2.4), mark in the service's light tone. Where a brand kit requires the original mark color, honor the kit; the chip background adapts.
- Now Playing pairs the logo chip with the service name in a labeled pill. Cards use the bare chip.
- Logos identify source, never act as buttons. Tapping a card plays content; it never deep-links to the service's own app.

---

## 6. Core Components

### 6.1 Art card
Artwork square, `radius-art`, title in `body` below, artist/meta in `caption`, service badge chip overlaid bottom-left of the art at 8px inset. Pressed state: scale 0.97, `bg-overlay` behind. No hover-lift shadows.

### 6.2 Recents rail
Horizontal scroll of art cards. Each card shows a small target glyph (amp or speaker) in the corner indicating where it last played. Tap opens the target picker with the last-used target pre-highlighted; confirming plays. The common case is two fast taps, and content never surprises the wrong room.

### 6.3 Mini-player (persistent)
Pinned bottom bar, 64px tall, `bg-raised`, `radius-sheet` top corners. Contents: 44px art thumb, title + artist stacked (marquee allowed), play/pause, and the **zone dot cluster**: one dot per active output, amber-lit when live. Past 4 active outputs the cluster collapses to a count chip ("5 rooms"). Tap anywhere expands to Now Playing with a shared-element transition on the artwork. Swipe down collapses.

### 6.4 Now Playing
- Backdrop: hero art blurred 60px, tinted with `art_accent` at 40 percent over `bg-base`.
- Hero art upper two-thirds, then title (`display`), artist (`caption`, `text-secondary`), service badge.
- Scrubber: 4px track at rest, thickens to 8px on touch. Fill uses `art_accent`, track uses `stroke`. While scrubbing, a **cue bubble** floats above the thumb showing target timecode in `micro` tabular figures; commit on release. Elapsed/total timecodes sit at the track ends.
- Transport row: prev, minus 15, play/pause (64px primary), plus 15, next. **Hit targets are fixed at 56px squares** (icon centered, visual size smaller), the row is capped at 372px and space-distributed, guaranteeing at least 18px of dead space between adjacent targets at phone width. Adjacent interactive targets anywhere in the app maintain a minimum 8px gap; transport gets more because mis-taps there are the costliest.
- Seek affordances (scrubber thumb, ±15) render disabled at 40 percent opacity for radio sources (Pandora), per PRD CTL notes.

### 6.5 Volume sheet
Bottom sheet listing each device: glyph, name, horizontal slider. Linked master slider on top, separated by a hairline. Sliders are 4px tracks with 28px thumbs, fill in `signal`. Sonos and HEOS devices are interleaved in one list, distinguished by glyph only. No brand grouping; the user thinks in rooms.

### 6.6 Zone/target picker
Bottom sheet, one row per output: glyph, room name, power state. HEOS zones show a power toggle; Sonos rows show playing/idle (no power concept, per PRD ZON-2). Multi-select with amber check states feeds Sync Play.

### 6.7 Sync chip
Pill chip visible on Now Playing and mini-player during Sync Play:
- `sync-locked` dot + "Synced"
- `sync-drift` dot + "Adjusting" (pulses once per correction event, subtle)
- `sync-lost` dot + "Sync lost", tap to retry
Copy stays honest. The chip is the UI expression of the PRD's "close, not perfect" stance.

### 6.8 Buttons
- Primary: `bg-overlay` fill, `text-primary` label, `radius-control`, 48px height.
- The only amber-filled button in the app is **Sync Play**. The accent means "live audio state," and this button creates one.
- Destructive/stop actions use plain text buttons in `error`. No red fills.

---

### 6.9 Settings
Full-screen push from the right (gear icon in home header). Grouped `bg-raised` cards with `radius-control`, 56px minimum row height, hairline dividers inset past the icon column. Groups:
- **Accounts:** one row per service (logo chip 28px, status line "Connected · account"), plus the HEOS account row. Tap manages link/unlink.
- **Hub:** status row (address, version, uptime), check for updates, restart service (destructive rows use `error` text, no red fills).
- **Zones:** discovered hardware with glyph, vendor, and zone detail.
Destructive actions confirm via bottom sheet, never modal dialog.

## 7. Motion

One signature moment: the **mini-player expand**. Artwork shared-element morphs from the 44px thumb to the hero, backdrop blur fades in, 320ms spring. Everything else is functional:

- Scrubber thickening: 120ms ease-out on touch.
- Sync chip pulse: single 400ms opacity pulse on correction, never looping.
- Card press: 100ms scale.
- List/screen transitions: plain 150ms fades. No slide-up entrances on section load, no staggered card reveals.

`prefers-reduced-motion`: the expand becomes a crossfade, pulses become static color changes, marquee stops.

---

## 8. States

**Loading:** neutral skeleton blocks in `bg-raised`, no shimmer sweep. Artwork skeletons hold the square so grids never reflow.

**Empty:**
- No recents yet: "Play something and it lands here." with the browse sections directly below, since the grids themselves are the call to action.
- Service not linked: card in grid position reading "Connect Tidal" / "Connect YouTube Music", tap starts the hub sign-in flow.

**Errors:**
- Hub unreachable: full-width banner under the header, `error` text on `bg-raised`: "Can't reach the hub. Check that the Mac is on the network." Retries automatically; banner shows last-attempt time in `micro`.
- Command failed: toast, plain statement of what did not happen ("Volume didn't change on Living Room"), auto-dismiss 4s.
- Errors state facts and fixes. They never apologize.

**Offline device:** its rows and zone dots drop to `text-tertiary` with "offline" in `micro`. Never hidden; a missing speaker is information.

---

## 9. Accessibility

- Contrast: `text-primary` on `bg-base` is ~14:1; `text-secondary` ~6.5:1. The dynamic backdrop clamp (Section 2.2) exists to keep Now Playing text at 4.5:1 minimum against any album's tint. The hub validates contrast at extraction time and falls back rather than shipping an unreadable accent.
- Touch targets: 48px minimum everywhere, 56px on the transport row; adjacent targets keep at least 8px of separation. The scrubber gets an invisible 48px hit zone around the 8px visual track.
- Zone dots and sync states never rely on color alone: glyph shape plus label text carry the same information.
- Focus states (keyboard/remote): 2px `signal` ring at 2px offset.
- Timecodes and volume are announced as text to screen readers on change-commit, not continuously during drag.

---

## 10. Voice

- Sentence case, plain verbs, no filler. "Play on both", "Turn off Zone 2", "Connect Tidal".
- Rooms and devices are called what the user named them in the vendor apps; the hub inherits names, never renames.
- The sync feature never overclaims: "Synced" and "Adjusting", never "Perfect sync".
- One name per action across the whole flow: the button says "Sync Play", the chip confirms "Synced", history logs "Sync Play".

---

## 11. Implementation Notes

- Ship tokens as CSS custom properties in `:root`; the Tailwind config maps to them (`bg-base: var(--bg-base)`), so the design system lives in one file.
- `art_accent` arrives from the hub API with each artwork payload: `{url, accent, accent_is_safe}`. The client never computes color; extraction and the contrast check are hub-side (Pillow, cached with the image).
- General Sans self-hosted from the hub (Fontshare license permits); no third-party font CDN so the app works when the internet is down but the LAN is up. Fallback stack: `"General Sans", -apple-system, "Segoe UI", sans-serif`.
- Tabular numerals via `font-variant-numeric: tabular-nums` on a `.numeric` utility.
- Blur backdrop is a pre-blurred image variant served by the art proxy (a second cache entry), not a runtime CSS `filter: blur()` on the hero, which is expensive on wall-mounted tablets.

---

## 12. Resolved Decisions

1. **Recents card size:** 148px on phones, tuned against real artwork during Phase 2. Build-time adjustment, not a spec change.
2. **Zone dot cluster:** collapses to a count chip ("5 rooms") past 4 active outputs (Section 6.3).
3. **Theme:** v1 ships dark-only. The token layer makes a light theme additive later.
4. **Transport:** full 5-button row retained, 56px hit targets, per Section 6.4.
5. **Recents interaction:** tap always opens the target picker, last-used target pre-highlighted (Section 6.2).

---

## 13. Changelog

### 1.1 — September 7, 2026 (Phase 2 build and UX review)

Refinements found while implementing Phase 2. Each is a clarification of intent, not a change of direction.

1. **Scrubber fill fallback.** §2.2 says `art_accent` falls back to `bg-raised`. That is correct for the backdrop tint only. `bg-raised` is darker than the `stroke` track, so it cannot be the scrubber fill. Fill fallback is `text-primary`. The client also guards live: if contrast(accent, `stroke`) is under 3:1, the fill uses `text-primary`.
2. **Non-seekable scrubber.** §6.4 "disabled at 40 percent opacity" applies to the thumb and the ±15 buttons only. Track, fill, and timecodes stay at full opacity so position still reads. The thumb is hidden entirely (a progress bar without a handle reads as display, not as a broken slider). The control stays focusable with `aria-readonly`. This now also covers HEOS sides, since the HEOS protocol has no seek command (PRD review §6a).
3. **`accent_is_safe` semantics.** The hub returns `accent_is_safe=false` and `accent=null` when extraction fails, when the artwork is effectively greyscale (a grey tint is pointless), or when the clamped accent fails the contrast check. After the §2.2 clamp the contrast check almost always passes; the greyscale rule is what makes the flag meaningful in practice.
4. **Transport row on the smallest phones.** §6.4 assumed 372px usable. At 16px margins a 375px phone yields 343px and 13.75px gaps. The transport row alone uses a 12px margin on phones; the 8px hard minimum holds everywhere and the 18px target holds from 390px up.
5. **Alpha and scrim tokens.** Add `--bg-base-rgb: 16 16 20` (and matching `-rgb` triplets for `bg-raised`, `bg-overlay`) so Tailwind alpha modifiers work, plus `--scrim: rgb(16 16 20 / 0.6)` for sheets. Add `--size-sheet-handle: 40px` for the sheet drag pill.
6. **Landscape and wall tablet.** Now Playing caps the hero by height (`min(520px, 100dvh - 396px)`) and switches to a two-column layout (art left, controls right) at landscape widths of 700px and above. Play/pause must never require scrolling. Screen margins respect `env(safe-area-inset-left/right)`.
7. **Hub-unreachable banner placement.** The banner renders above every surface, including the expanded Now Playing, not only in the home shell.
8. **Denon zone toggles.** Each power toggle carries a `micro` label ("Main", "Zone 2") and the on state has a filled ring, so the two toggles never differ by color alone (§9).
9. **Zone dots.** Dots model live and online separately: amber live, `signal-dim` idle, `text-tertiary` offline, with a text label such as "2 rooms playing, 1 offline".
10. **Open questions filed as issues:** `text-tertiary` contrast versus §9 (illyHub #3), marquee trigger (#4), sheet modality (#5).

### 1.2 — September 7, 2026 (Phase 4 Sync Play build and UX review)

1. **Sync chip glyph.** One inline SVG glyph per state, coloured with the state token and carrying shape: filled circle "Synced", half circle "Adjusting", ring "Sync lost", dotted ring "Starting". No separate dot beside the glyph. The single 400ms pulse plays on the glyph.
2. **Sync labels.** Room names in sync copy are joined with " and " ("Syncing Living Room Amp and Kitchen + Patio"), because grouped sides are already named with "+". The Now Playing header shows "Syncing 2 rooms"; full names live in the accessible label and the picker.
3. **Stop sync tone.** "Stop sync" is a plain text button in `text-secondary`, not `error`: both rooms keep playing, so it is not a destructive action. §6.8's `error` rule applies to actions that stop audio.
4. **Lost state.** The chip reads "Sync lost" and is accompanied by a visible "Retry" text button (48px) on Now Playing; the mini-player uses a compact single button "Sync lost · Retry". Only one Stop affordance exists on a screen.
5. **Announcements.** Sync state is announced once per text change from a single hidden live region, never per drift tick; the transition to "Sync lost" is always announced.
6. **Picker copy for Sync Play.** Button "Sync Play" or "Sync Play in N rooms"; note "{HEOS rooms} and {Sonos rooms}, together. Close, not perfect." tied to the button with `aria-describedby`. A plain secondary "Play in N rooms" stays available under it so unsynced multi-room play remains possible.
7. **Offer placement.** The amber Sync Play offer never renders where "Stop sync" just sat, and is suppressed briefly after a stop, so a double tap cannot restart a session.

### 1.3 — September 7, 2026 (Phase 5 Pandora build)

1. **Radio timecodes.** §6.4's "elapsed/total timecodes sit at the track ends" applies to on-demand content. For radio sources with no known duration, only the elapsed time renders (tabular), and the scrubber is a progress display, not a slider. When a station does report a track duration, both timecodes render as usual.
2. **Pandora is linked in the vendor apps, not in the hub.** There is no "Connect Pandora" card. When a vendor has no Pandora (the hub reports `linked` per vendor), the home stations section carries one sentence naming the affected rooms: "Den and Living Room Amp can't play these until Pandora is added in the HEOS app." The picker row for that vendor reads "Pandora is set up in the HEOS app."; a linked vendor whose account lacks the station reads "Not in the HEOS Pandora account." Unavailability reasons render in `text-secondary`. Station cards carry an availability caption when one-sided ("HEOS rooms only"), otherwise no caption.
3. **Single-stream note.** When at least one room is selected and either two rooms are selected for a station or another room already plays Pandora, the picker shows one sentence above the confirm button: "Pandora usually allows one stream per account; the other room may pause." The hub's ack warning is shown as a toast that names the room ("Kitchen may pause: …"); identical toasts are merged. Stations never offer Sync Play: no disabled Sync Play button appears; when both vendors are selected a caption reads "Stations can't sync: Pandora picks different songs for each room."
4. **Station now-playing.** Track name is the title, station name takes the album line, Pandora badge pill as usual. Next is enabled (Pandora skips), previous is disabled, seek affordances follow the radio rule.

### 1.4 — September 7, 2026 (Phase 6 YouTube Music build)

1. **Unavailability copy follows the hub's reason, not the client's guess.** Every item carries `availability.reasons` per vendor. "unsupported" → "{Service} isn't available on {Vendor}." (a fact about the product); "not_linked" → "{Service} is set up in the {Vendor} app." (fact and fix); "not_in_account" → "Not in the {Vendor} {Service} account." The same sentence is used in the picker row, the detail view and toasts. YouTube Music is unsupported on HEOS in v1.
2. **Detail view availability.** When every track shares the container's availability, one caption in the header meta line says so ("You · 6 tracks · Sonos rooms only") and the rows keep their artists. Per-row notes appear only where a track differs from its container. The phrasing is always "{Vendor} rooms only".
3. **Connect cards.** One card per unlinked hub-linked service (Tidal, YouTube Music), rendered once per screen in the first section that needs it; never a card for a linked service. Pandora has no card; it is linked in the vendor apps.
4. **Picker pre-selection.** After filtering rooms that cannot play the content, if exactly one usable room remains it is pre-selected, so the common case stays two taps.
5. **Owner-only setup errors.** When the hub lacks OAuth client credentials for a service, the shared Settings row reads "Not connected · Hub setup needed"; the full instruction (which env vars to set) appears only inside the link sheet the owner opens, with a Close action instead of Try again.
6. **Link sheet focus.** When the sheet changes phase (connected, expired, error), focus moves to the sheet's primary action so keyboard and switch users keep their place.

### 1.5 — September 7, 2026 (Phase 7 Pandora Sync build)

1. **One name.** The AirPlay bridge feature is "Pandora Sync" in every surface: picker button "Pandora Sync (experimental)", chip "Pandora Sync", indicator "Pandora Sync · N rooms", Settings row "Pandora Sync (AirPlay bridge)". Never "Sync via AirPlay" or "Pandora via AirPlay".
2. **Explain the hand-off before the tap.** The picker note tied to the button reads: "The hub Mac plays this station to {rooms} over AirPlay. It opens Pandora there; press play on the Mac." "Experimental" appears once per surface and only next to that sentence.
3. **Plain text, never amber.** Pandora Sync hands audio off to the Mac; the hub does not control that playback, so no amber anywhere in the feature, including the read-only Outputs sheet (neutral check glyphs, non-tappable rows).
4. **Bridged Now Playing.** Transport, seek and the primary disc are disabled (disc outlined, not filled); one full-width caption under the meta: "Press play on the hub Mac; the rooms follow. Controls are there while Pandora Sync is on." Room volume stays live. Only the rooms in the session are bridged; other rooms keep full controls. One Stop, in the meta row, in `error` tone, because stopping silences the rooms.
5. **Mini-player chip is status only.** It never acts; tapping falls through to expand. The stop note from the hub is toasted once.
6. **Scope statement in Settings.** "Experimental. Pandora Sync points the Mac's AirPlay outputs at your rooms and opens Pandora there; someone presses play on the Mac." in caption/secondary. Reasons for unavailability wrap rather than clamp.
7. **Deployment reality.** With the hub running as a root daemon the bridge reports unavailable with a reason; Pandora Sync works only from a console-session hub process until the user-session helper exists (illyHub #28).

### 1.6 — September 8, 2026 (Phase 8 cross-cutting build)

1. **Options row.** Shuffle · Up next · repeat sit under the transport row as secondary controls (48px targets, never amber). On-state is a `bg-overlay` pill plus a 4px `text-primary` dot under the glyph, so state is carried by shape as well as fill. Repeat cycles off → all → one; the "one" glyph carries a legible digit. The row is hidden below 420px viewport height, while a room is bridged by Pandora Sync, and for stations; during Sync Play the toggles are disabled with the caption "Shuffle and repeat are off during Sync Play." in a fixed-height slot so the transport never moves.
2. **Up next.** A plain-text button, not a gesture. The sheet highlights the current track (amber title, `aria-current`) and scrolls it into view on open.
3. **Search.** A magnifier in the home header expands to a 48px field with a text "Cancel"; the native clear control is hidden. Results replace the home sections in the same main region and restore the scroll position on Cancel. One `role="status"` line announces the result count, empty state ("Nothing matched.") or a partial-results sentence; result trees are not live regions.
4. **Hub update row.** "Check for updates" is live. Copy never shows raw tool output ("Couldn't check for updates. Tap to try again."). The confirm sheet says the hub restarts, control drops for about a minute, the screen reconnects on its own, and a failed update rolls back. Self-update is off by default on the hub; the row then reads the hub's "Updates are turned off on this hub." and is not tappable.
5. **Hub stats.** A text disclosure ("Show stats / Hide stats") with one household-facing sentence from the hub ("Running since 7:37 today. 10 commands, all worked."). Engineering numbers live in the metrics API, not in Settings.
