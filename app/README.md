# illyHub app

Touch PWA for the illyHub hub. Next.js static export, TypeScript, Tailwind mapped to the design
tokens in `src/styles/tokens.css`. The hub serves the export at `/`; the app talks to the hub on
the same origin over REST (`/api/*`) and WebSocket (`/ws`). Contract: `../docs/api.md`.

## Develop

```bash
npm install
npm run dev                      # http://localhost:3000, expects a hub on the same origin…
NEXT_PUBLIC_HUB_URL=http://127.0.0.1:8080 npm run dev   # …or point it at a hub elsewhere
```

Run a hub with fake devices for development:

```bash
cd ../hub && HUB_FAKE_DEVICES=1 uv run hub
```

Run it with the canned Tidal library so home, browse, history and settings have data:

```bash
cd ../hub && HUB_FAKE_DEVICES=1 HUB_FAKE_TIDAL=1 uv run hub
```

`/dev/tokens` renders every design token for visual QA (development builds only).

## Build and serve from the hub

```bash
npm run build                    # writes out/
cd ../hub && HUB_APP_DIR=../app/out uv run hub   # the hub mounts out/ at / with SPA fallback
```

The hub also serves `public/fonts/` (General Sans, Fontshare license, self-hosted) so the app
works with the internet down.

## Test

```bash
npm run typecheck                # tsc --noEmit
npm run lint                     # eslint
npm test                         # vitest, jsdom
npm run coverage                 # vitest --coverage; thresholds: lines 80, branches 75, functions 80
npm run e2e                      # playwright: builds, starts the fake hub (fake devices + fake Tidal), runs WebKit (iPhone 13) + Chromium (Pixel 7)
```

E2E specs (WebKit iPhone 13 + Chromium Pixel 7, run serially because the fake hub is shared state):

- `e2e/smoke.spec.ts`: snapshot load, play/pause toggle, volume sheet, live track and volume changes.
- `e2e/viewports.spec.ts`: the transport row fits without scrolling on iPhone SE, iPhone 13
  landscape, iPad landscape, Pixel 7.
- `e2e/home.spec.ts`: home grids from the canned Tidal library; album tap → play-mode picker →
  mini-player + Recently played; the two-tap recents flow (the app records
  `performance.mark("play:confirm")` / `"play:ack"` and the test reads the measure; the sub-second
  bound is asserted locally, not on CI); browse detail play-from-track (hub `index`); Settings
  unlink → connect cards → device-code relink approved via the `approve_tidal` dev scenario.

`src/styles/classes.test.ts` builds the real Tailwind output and fails if any class used in a
`className` produced no CSS (catches theme/token drift). `e2e/viewports.spec.ts` asserts the
transport row never needs scrolling on iPhone SE portrait, iPhone 13 landscape, iPad landscape,
and Pixel 7.

## Playing content (PRD Decision 4)

Tapping any card or track never plays immediately: it opens the target picker in play mode with the
last-used rooms pre-highlighted (hub history first, `localStorage` per content ref second). The
confirm button posts one `POST /api/play` per chosen side, patches the mini-player optimistically,
and refreshes home so the item lands in Recently played. Sides that cannot play the content
(`availability`) render disabled with the reason.

## Brand marks

`public/brands/LICENSES.md` records where the official Tidal / YouTube Music / Pandora marks come
from and their usage terms. Until they are downloaded (they sit behind brand-kit pages that block
automated fetches), `ServiceBadge` renders stand-in glyphs; flip `OFFICIAL` once the SVGs are in
`public/brands/`.

## Types

`src/lib/hub/openapi.d.ts` is generated from the hub's OpenAPI document:

```bash
npm run openapi                  # regenerates openapi.json + openapi.d.ts from ../hub
```

WebSocket-only shapes (`HubState`, `NowPlaying`, `Position`) are hand-mirrored in `src/lib/hub/types.ts`.

## Layout

```
src/app/            routes (App Router, static export): / (home), /browse?ref=service:kind:id
                    (detail), /settings (accounts, hub, zones; ?link=tidal starts the link flow),
                    /dev/tokens
src/components/     PlayerChrome (mini-player + Now Playing overlay + target picker, mounted in the
                    layout for every route), HomeScreen, ArtCard, RecentsRail, BrowseDetail,
                    VirtualList, SettingsScreen, NowPlaying, MiniPlayer, Scrubber, TransportRow,
                    VolumeSheet, ZonePicker (default + play mode), Sheet, Slider, Banner, Toasts,
                    icons, ServiceBadge
src/lib/hub/        store (zustand), socket, commands (REST), library (browse/history/home/
                    settings/auth client + normalisers), state (delta apply), types, config
src/lib/library/    SWR-style store for home, details, settings (refetch on focus and after plays)
src/lib/ui/         chrome state (Now Playing expanded, picker, play request), toasts
src/lib/            position interpolation, scrub math, coalescer, prefs, selectors, format
src/styles/         tokens.css (single source of truth), globals.css
e2e/                Playwright: smoke, viewports, home, sync, pandora
```

## Sync Play (Phase 4)

One HEOS side (clock master) plus one Sonos side (follower) playing the same Tidal content. The
hub owns the engine (`docs/api.md` "Sync Play", `docs/sync-engine.md`); the app only decides when
to offer it and renders `HubState.sync`.

- **Picker rule** (`src/lib/sync.ts` `syncEligibility`): at least one HEOS and one Sonos side
  selected and Tidal content → the confirm button becomes **Sync Play**, the only amber-filled
  button in the app, with the note "Close, not perfect." Both vendors but non-Tidal content →
  plain Play plus a disabled Sync Play with the reason. One vendor → plain Play, nothing else.
- **Now Playing** offers Sync Play (amber, secondary) when the active side plays Tidal and the
  other vendor has an online side; it opens the picker with both pre-selected. During a session
  the indicator reads "Syncing A + B", the scrubber is read-only (HEOS cannot seek), and a plain
  "Stop sync" text button returns both rooms to independent control.
- **Sync chip** (`SyncChip`, design §6.7): Starting / Synced / Adjusting / Sync lost from
  `sync.status`, one 400 ms pulse per correction (keyed on `last_correction_at`), Retry and Stop
  when lost. Shown next to the indicator and in the mini-player.
- **Transport during a session** goes to the master side and the hub mirrors it to the follower
  while the session is being driven (resolving through correcting); once lost or stopped each
  side is on its own again (`transportTargetFor` in `src/lib/sync.ts`).
- E2E: `e2e/sync.spec.ts` drives the fake hub's `sync_drift` / `sync_lose_sonos` scenarios.

## Pandora (Phase 5)

Stations are per ecosystem and Pandora is linked inside the HEOS and Sonos apps, never through
the hub, so there is no Connect flow. The hub merges both vendors' station lists and reports
per-station availability (`docs/api.md`, Phase 5); the app only renders that.

- **Home** (`stationsSectionModel` in `src/lib/pandora.ts`): a "Pandora stations" section of
  art cards with the Pandora badge, alphabetical, hidden while empty (a hub-side section error
  shows in its place). A vendor the hub reports as not linked (`stations.linked`) gets a one-line
  note naming its rooms ("Living Room Amp and Den can't play these until Pandora is added in the
  HEOS app.") instead of a Connect card. Card subtitles come from availability ("Sonos rooms only";
  none when both vendors can play). Station cards have no detail view; tapping opens the target picker.
- **Picker**: rows whose vendor cannot play the station are disabled with the reason from the hub's
  per-vendor link state: "Pandora is set up in the HEOS app." when that vendor is not linked, "Not in
  the HEOS Pandora account." when it is linked but lacks the station. Sync Play is never offered for
  stations: with both vendors selected the picker shows one caption ("Stations can't sync: Pandora
  picks different songs for each room.") and no Sync Play button. Choosing two rooms, or one room
  while another already plays Pandora, shows the single-stream note (PRD §3.5); the hub's
  `pandora_concurrent` note on `Ack.warnings` (ok stays true) is toasted, leading with the room the
  hub names ("Kitchen may pause: …"); identical toasts refresh instead of stacking.
- **Now Playing radio mode**: the scrubber is read-only with no thumb, elapsed time only (no
  total when `duration_ms` is null), previous disabled and next enabled per the hub's
  capabilities, Pandora badge, station name on the album line. Recents show stations with the
  badge and the last-played glyph.
- E2E: `e2e/pandora.spec.ts` against the fake hub's canned stations (`HUB_FAKE_PANDORA=1`) and
  the `link_pandora` / `unlink_pandora?vendor=` scenarios.
