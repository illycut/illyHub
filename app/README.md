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

## Android APK (Capacitor shell)

[`../docs/android.md`](../docs/android.md) covers it end to end. Short version: the export in `out/` is bundled into an
APK by `npm run android:build` (needs a JDK 17 and the Android SDK; CI does it on every push to
`main` and attaches `app-debug.apk` to the rolling `android-latest` pre-release). On first launch
the shell asks for the hub address (`http://<hub-ip>:8080`), stores it in localStorage
(`src/lib/hub/hubBase.ts`), and everything else is the same PWA. Change or forget the address
under Settings → Hub → Hub address. The browser PWA remains the zero-install path.

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
- `e2e/airplay.spec.ts` (Phase 7, `HUB_AIRPLAY_ENABLED=1`): Settings AirPlay row and read-only outputs
  sheet; station → picker → "Pandora Sync (experimental)" → chip on the mini-player and Now Playing with
  the disabled-transport caption → Stop → chip gone; the affordance never appears for non-station content.
- `e2e/phase8.spec.ts` (Phase 8): Up next queue jump on the Sonos side (the hub's `index` rides in the
  body and the title follows), search "Harmonic" from the home header → grouped canned results → picker →
  play, shuffle round-trip confirmed through `/api/devices`, and the live "Check for updates" row plus the
  Hub stats disclosure.

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
                    (detail), /settings (accounts, hub, zones; ?link=tidal|ytmusic starts that
                    service's link flow unless it is already linked), /dev/tokens
src/components/     PlayerChrome (mini-player + Now Playing overlay + target picker, mounted in the
                    layout for every route), HomeScreen, ArtCard, RecentsRail, BrowseDetail,
                    VirtualList, SettingsScreen, NowPlaying, MiniPlayer, Scrubber, TransportRow,
                    PlayOptionsRow (shuffle · Up next · repeat), QueueSheet, Search (header field +
                    results view), VolumeSheet, ZonePicker (default + play mode), LinkSheet (device-code
                    flow shared by Tidal and YouTube Music), Sheet, Slider, Banner, Toasts, icons, ServiceBadge
src/lib/hub/        store (zustand), socket, commands (REST), library (browse/history/home/
                    settings/auth client + normalisers), state (delta apply), types, config,
                    openapi.json / openapi.d.ts and messages.json (both generated by `npm run openapi`)
src/lib/library/    SWR-style store for home, details, settings (refetch on focus and after plays)
src/lib/ui/         chrome state (Now Playing expanded, picker, play request), toasts
src/lib/            services (labels, hub-linked services, unavailability sentences from the hub's
                    messages), pandora, sync, airplay, queue, playMode, search, update (row state
                    machine), position interpolation, scrub math, coalescer, prefs, selectors, format
src/styles/         tokens.css (single source of truth), globals.css
e2e/                Playwright: smoke, viewports, home, sync, pandora, ytmusic, airplay, phase8
```

## Volume ownership and quantisation (PRD §3.3 [1.2])

Who owns a player's volume is the hub's call, not the app's (`docs/api.md` Concepts). A HEOS player
hosted by a Denon AVR has no usable volume of its own; the amplifier zone is the authority, and the
hub mirrors the zone's level onto the player. The app renders that mirrored number and never shows
the zone twice: a zone that owns a player (`Zone.player_ids`) is represented by the player's row.

- **Zones as targets.** A zone that owns no player (a Zone 2 driving an external amp) gets its own
  row in the volume sheet: a slider posting `POST /api/volume {target: zone_id}` and a mute toggle
  when `supports_mute`. Store action: `setZoneVolume(zoneId, level)`; `setMute` accepts a zone id
  and mirrors onto the players the zone owns.
- **Fixed outputs.** A zone with `supports_volume: false` (a fixed pre-out) shows its power state
  only — no slider, no mute — matching the vendor app, which offers it as power-only. If a command
  reaches the hub anyway it answers `unsupported_action`; the app toasts that sentence verbatim as a
  backstop.
- **Quantised levels.** Hub `0–100` maps onto the receiver's step scale, so a request for 96 can
  settle at 97. The slider always renders the level the hub reports after the ack; a few steps of
  difference is not a failed command and never triggers the revert path.
- Picker zone rows keep their power toggles and offer no volume; the Now Playing side slider is
  unchanged. Tests: `src/components/Volume.test.tsx`.

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

## Pandora Sync (Phase 7, experimental)

PRD PAN-4. The hub Mac plays Pandora itself and streams to AirPlay 2 outputs; the hub only
manages the routing (`docs/api.md` "AirPlay bridge and Pandora Sync"). The app never controls
that playback. One name everywhere: "Pandora Sync".

- **Capability-gated.** `GET /api/settings` → `hub.airplay {enabled, available, reason}`
  (PlayerChrome loads settings on mount so a deep link straight into a station has it). Settings →
  Hub always shows a "Pandora Sync (AirPlay bridge)" row reading Available / Unavailable · reason
  (wrapping, never clamped) / Off, plus the experimental disclosure; when available, a read-only
  "Outputs" sheet lists `GET /api/airplay` outputs with neutral checks mirroring `selected` and the
  hub's `kind_label` ("This Mac", "AirPlay speaker"). Outputs are chosen on the hub Mac in v1.
- **Affordance.** In the picker, for a Pandora station only and never while a Sync Play session
  is live, a plain text button "Pandora Sync (experimental)" sits under the confirm button with a
  note tied by `aria-describedby` ("The hub Mac plays this station to {rooms} over AirPlay. It opens
  Pandora there; press play on the Mac."). It is never amber: amber means a live audio state the
  hub drives (Sync Play). Tapping it posts `/api/pandora-sync/start {side_ids}` with every chosen
  room; the hub matches rooms to outputs and refuses, naming the rooms, when one has no output.
  Refusals (`bridge_unavailable`, `invalid_argument`, `sync_active`) are toasted verbatim. No
  optimistic state: the hub's `pandora_sync` delta drives the chip; the hub's started note is
  toasted once. Sync Play is hidden while Pandora Sync is on.
- **While a room is bridged** (`pandora_sync.side_ids`; other rooms stay fully live): a neutral
  status chip "Pandora Sync" on Now Playing and inside the mini-player's expand button; the target
  indicator reads "Pandora Sync · N rooms" and names them in its label; the badge is dropped (the
  hub's last track is stale); transport and ±15 are disabled with the primary disc outlined, the
  scrubber is read-only and its tick is off, all described by one full-width caption: "Press play
  on the hub Mac; the rooms follow. Controls are there while Pandora Sync is on." The room's own
  volume slider and the volume sheet keep working. The single Stop (text, error tone) sits in the
  meta row and posts `/api/pandora-sync/stop`; the hub's stopped note is toasted once; a stop while
  idle (`sync_idle`) is not an error.
- **Play state.** The contract's states are play / pause / stop / unknown (the hub holds the last
  known state across a device's transient). One predicate (`src/lib/playState.ts`, `isPlaying`)
  decides "live" everywhere (dots, active side, icon, scrubber tick, interpolation). The store
  holds the user's last transport intent per side until the hub reports a settled state, so a
  transient `unknown` never flips the icon.
- Code: `src/lib/airplay.ts` (rules and copy from `messages.json` `templates.airplay.*`),
  `src/components/PandoraSyncChip.tsx`, the picker, Now Playing, mini-player and Settings wiring;
  store actions `pandoraSyncStart(sideIds)` / `pandoraSyncStop`. Tests: `src/lib/airplay.test.ts`,
  `src/components/AirPlay.test.tsx`, `e2e/airplay.spec.ts` (fake hub with `HUB_AIRPLAY_ENABLED=1`).

## YouTube Music (Phase 6)

YouTube Music is the second hub-linked service. Settings → Accounts → YouTube Music runs the same
device-code flow as Tidal through one shared component (`src/components/LinkSheet.tsx`, a view of a
`LinkPhase`, parameterised by service; polling with the hub's `interval_s` and `expires_in_s` lives in
`SettingsScreen`). The "Connect YouTube Music" card on home deep-links to `/settings?link=ytmusic`,
which starts the flow once settings are known unless the account is already linked. When the hub
lacks Google OAuth client credentials it answers `409 needs_client_config`: the row reads
"Not connected · Hub setup needed" and the sheet shows the hub's full sentence with a Close button
(nothing for the user to retry).

Copy comes from the hub. `npm run openapi` writes `src/lib/hub/messages.json` from
`GET /api/meta/messages` (`uv run python -m illyhub_hub.messages`): service and vendor labels, the
vendor app names, the unsupported-on-vendor table, the concurrent-stream and Sync Play sentences, and
the templates. `src/lib/services.ts` builds every unavailability sentence from the hub's reason code on
the item (`availability.reasons[vendor]`): `unsupported` → "YouTube Music isn't available on HEOS."
(the hub's template), `not_linked` → "Tidal is set up in the HEOS app.", `not_in_account` → "Not in the
Sonos Pandora account.", `auth_fault` → "Sonos can't reach Pandora right now; sign in again in the Sonos
app." The contract tests compare against `messages.json`, never against docs prose.

Home merges YouTube Music playlists into "Your playlists" and YouTube Music albums into "Favorite
albums" (the hub sorts by recency then title; the badge tells services apart). Connect cards come from
each section's `needs_link` list plus, belt and braces, any hub-linked account `/api/settings` reports
unlinked; each card renders once per screen, in the first grid, never for a linked service. In the
picker every room's state comes from the item's `availability` and `reasons`; while the hub reports
`heos: false` / `unsupported` for YouTube Music, HEOS rows are disabled at full opacity with the hub's
sentence and the only playable room is pre-selected (two taps). If the HEOS spike ever lands and the hub
flips `availability.heos`, the rows open with no app change. Sync Play never runs for YouTube Music:
with both vendors selected the picker shows the disabled Sync Play button and the hub's
"Sync Play works with Tidal content." (design system §6.6, §6.8; stations differ, see Phase 5). Browse
detail shows one header caption when a container is uniformly one-sided ("Sonos rooms only"), keeps
artists on every row, and adds a short non-truncating note only on rows that differ; containers the hub
cut at its track cap carry "Showing the first N tracks."

E2E: `e2e/ytmusic.spec.ts` runs against `HUB_FAKE_YTMUSIC=1` (dev scenarios `link_ytmusic`,
`unlink_ytmusic`, `approve_ytmusic`); every spec relinks both services afterwards.

## Up next, search, shuffle/repeat, updates (Phase 8, cross-cutting)

Contract: `docs/api.md` sections "Queue", "Search", "Play mode", "Update" and "Metrics". The client
never re-derives hub decisions; it renders `HubState` and the REST answers.

**Up next** (ai-dev #77). A plain-text "Up next" button sits between the shuffle and repeat toggles
under the transport row and opens a sheet listing `GET /api/queue/{side}` for the active side. The
playing entry is highlighted the way the detail view marks the current track (amber title,
`aria-current`); the hub's `current_index` wins, else the entry is matched by track id, else by
title + artist. A tap posts `POST /api/queue/jump {target, index}` with the hub's canonical index,
closes the sheet, and patches optimistically (side playing, now-playing shows the entry, intent held
across a transient `unknown`). Live changes arrive on the `queues.<side>` delta path (a depth-two
collection like `players`); a hub that has not streamed a queue is asked once per open, and a failed
fetch settles to "Nothing queued." rather than a skeleton. Radio sources show "Stations don't have a
queue." Truncated queues carry "Showing the first N tracks." The button is a button, not a swipe-up:
the Now Playing layer already owns drag-down and the sheets own drag-to-dismiss, and a labelled
control is the only discoverable form for keyboards and assistive tech.

**Search** (ai-dev #78). The magnifier in the home header expands to a full-width 48px field with
autofocus; Escape or Cancel collapses and clears. Queries under two characters are not sent; typing
debounces 300 ms and stale answers are dropped by run id. While a query is active the results replace
the home sections (grouped Albums / Playlists / Tracks / Stations as art cards and 56px rows with
service badges and "… rooms only" captions); every tap follows the picker rule. Per-service failures
from `errors` become one caption ("Tidal didn't answer; showing YouTube Music results."); nothing at
all is "Nothing matched."

**Shuffle / repeat** (ai-dev #79). Two 48px toggles under the transport row read the generated
`Side.play_mode` (`{shuffle, repeat: off|all|one}`; absent on older hubs reads as both off) and post
`POST /api/playmode {target, shuffle? | repeat?}` with an optimistic patch that reverts locally when
the hub refuses. Repeat cycles off → all → one. Accessible names are "Shuffle" / "Repeat" / "Repeat
all" / "Repeat one" with `aria-pressed`; the on state is a `bg-overlay` pill plus a dot under the glyph
(shape, not fill alone; amber stays reserved for live audio the hub drives). During Sync Play both are
disabled with one caption (the hub's `play_mode_locked` sentence); stations hide them and keep Up next.

**Check for updates** (PRD SET-2). The Settings → Hub row checks `GET /api/hub/update/check` on mount
(once per five minutes per session): "Up to date · {version}" or "Update available · {summaries}". A
failed check shows the hub's one sentence for it (`templates.hub.update_check_failed`; the row stays
tappable to retry) and never the check's raw `error` text. Tapping an available update confirms in a bottom sheet (it says control drops for about
a minute), then `POST /api/hub/update/apply` starts the job and the row polls `GET /api/hub/update/status`
every 2 s: `running` → "Updating the hub…" (adopting the job id); `succeeded` → the hub is about to exit
→ "Restarting the hub. This screen reconnects on its own." until the socket leaves open and returns,
then "Updated {before} → {after}" from the refreshed settings; `idle` for our job with a message
("already up to date") → re-check with no restart; `failed` / `rolled_back` settle the row with the
hub's message + log tail behind a "Show the log" disclosure that folds when the state changes. A
restart is only assumed once corroborated: the socket actually left open, or two consecutive status
polls failed; then no socket within 90 s → "The hub didn't come back after the update. Check the Mac."
Refusals by code: `update_running` adopts the live job; `update_disabled` (the hub's default,
`HUB_ALLOW_UPDATE=0`) renders the hub's own sentence as a plain, non-tappable row, never an error;
anything else (e.g. a refused remote URL) shows the hub's message verbatim. The machine is pure
(`src/lib/update.ts`) and unit-tested.

**Hub stats** (ai-dev #73). A "Show stats / Hide stats" text disclosure under the Hub group fetches
`GET /api/metrics/summary` (JSON `{summary}` or plain text) on open and shows the hub's household-facing
paragraph. No charts.

**Where the options row is not shown.** The shuffle · Up next · repeat row is hidden while the room is
bridged by Pandora Sync (the hub Mac owns playback) and on viewports under 420px tall (phones in
landscape), where it would push play/pause off screen; it returns in portrait and on the wall tablet.
Its caption slot has a fixed height so play/pause never moves when Sync Play starts.
