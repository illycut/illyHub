# Backlog — Multi-Room Audio Control App

Mirror of the GitHub issue backlog on `illycut/ai-dev`, generated September 7, 2026. GitHub is the source of truth for status; this file is for reading the plan in one place.

Labels: `hub` (Python service), `app` (PWA client), `infra`, `design`, `spike` (time-boxed, go/no-go), `P0`/`P1`/`P2`. One milestone per PRD build phase plus `Cross-cutting`.

| Milestone | Issues | Hub | App |
|---|---|---|---|
| Phase 0 — Foundation | 12 | 9 | 0 |
| Phase 1 — Core control | 9 | 9 | 0 |
| Phase 2 — Now Playing UI | 19 | 4 | 15 |
| Phase 3 — Tidal browse + Home | 13 | 6 | 7 |
| Phase 4 — Sync Play | 7 | 5 | 2 |
| Phase 5 — Pandora | 4 | 3 | 1 |
| Phase 6 — YT Music | 4 | 3 | 1 |
| Phase 7 — AirPlay bridge | 3 | 2 | 1 |
| Cross-cutting | 10 | 5 | 4 |
| **Total** | **81** | **46** | **31** |

## Suggested order of attack

1. #9 and #10 first. Both are under a day and gate Sync Play (Phase 4) and the Phase 3 play architecture.
2. Phase 0 hub items (#1–#8, #12) in parallel with the app scaffold and tokens (#26, #27) so the client can develop against fakes.
3. Phase 1 and Phase 2 together: the hub WebSocket (#18) unblocks every app screen.
4. #25 (HTTPS serving) before anyone installs the PWA on a phone.
5. #81 (PRD v1.1) once the two spikes report, so the PRD reflects reality before Phase 3.

## Phase 0 — Foundation

_Hub scaffold, discovery, persistent connections, state model, health endpoint. Exit: both systems discovered; state visible via API. Includes the two go/no-go gates from the PRD review._

- [#1](https://github.com/illycut/ai-dev/issues/1) **P0** [hub] Scaffold hub service (FastAPI, uv, config, logging)  
  `hub`
- [#2](https://github.com/illycut/ai-dev/issues/2) **P0** [hub] Device discovery: SSDP for HEOS and Sonos with static-IP fallback  
  `hub`
- [#3](https://github.com/illycut/ai-dev/issues/3) **P0** [hub] HEOS connection manager (pyheos): single persistent socket, keepalive, reconnect  
  `hub`
- [#4](https://github.com/illycut/ai-dev/issues/4) **P0** [hub] Sonos connection manager (SoCo): UPnP event subscriptions with callback listener  
  `hub`
- [#5](https://github.com/illycut/ai-dev/issues/5) **P0** [hub] Denon control link for zone power (HTTP primary, telnet secondary)  
  `hub`
- [#6](https://github.com/illycut/ai-dev/issues/6) **P0** [hub] Unified state model with 'side' abstraction  
  `hub`
- [#7](https://github.com/illycut/ai-dev/issues/7) **P0** [hub] GET /api/devices and GET /api/health  
  `hub`
- [#8](https://github.com/illycut/ai-dev/issues/8) **P0** [infra] launchd LaunchAgent, caffeinate, log rotation, deploy script  
  `infra`
- [#9](https://github.com/illycut/ai-dev/issues/9) **P0** [spike] Concurrent-stream check: Tidal (and YT Music) on HEOS + Sonos at once  
  `spike`
- [#10](https://github.com/illycut/ai-dev/issues/10) **P0** [spike] Tidal ID → HEOS-playable and Sonos-playable refs  
  `spike, hub`
- [#11](https://github.com/illycut/ai-dev/issues/11) **P1** [infra] Hub Mac runbook (macOS config checklist)  
  `infra, documentation`
- [#12](https://github.com/illycut/ai-dev/issues/12) **P1** [hub] Protocol fakes for HEOS, Sonos, and Denon (offline dev and tests)  
  `hub`

## Phase 1 — Core control

_Volume, play/pause, prev/next, seek, zone power, native grouping, WebSocket push. Exit: full transport + volume from curl/basic UI._

- [#13](https://github.com/illycut/ai-dev/issues/13) **P0** [hub] CTL-1/CTL-2 Command router + POST /api/transport/{action}  
  `hub`
- [#14](https://github.com/illycut/ai-dev/issues/14) **P0** [hub] CTL-3/CTL-4 Relative skip ±15s and POST /api/seek with capability flags  
  `hub`
- [#15](https://github.com/illycut/ai-dev/issues/15) **P0** [hub] VOL-1/VOL-3 Per-device volume and mute (POST /api/volume, POST /api/mute)  
  `hub`
- [#16](https://github.com/illycut/ai-dev/issues/16) **P1** [hub] VOL-2 Linked master volume (proportional)  
  `hub`
- [#17](https://github.com/illycut/ai-dev/issues/17) **P0** [hub] ZON-1/ZON-2 POST /api/zone/power with Sonos 'off' semantics  
  `hub`
- [#18](https://github.com/illycut/ai-dev/issues/18) **P0** [hub] WebSocket /ws: snapshot on connect, deltas, heartbeat, multi-client  
  `hub`
- [#19](https://github.com/illycut/ai-dev/issues/19) **P0** [hub] NP-3 Position tracking: Sonos 1Hz poll + HEOS progress events, sub-second estimation  
  `hub`
- [#20](https://github.com/illycut/ai-dev/issues/20) **P0** [hub] Native grouping: Sonos group coordinator and HEOS groups as sides  
  `hub`
- [#21](https://github.com/illycut/ai-dev/issues/21) **P1** [hub] Command acks, error codes, and failure toasts contract  
  `hub`

## Phase 2 — Now Playing UI

_PWA shell, design tokens, art proxy, Now Playing, scrubber, transport, mini-player, HTTPS serving. Exit: daily-drivable for basic control._

- [#22](https://github.com/illycut/ai-dev/issues/22) **P0** [hub] NP-1 Art proxy: fetch, disk cache (30-day TTL), size variants, pre-blurred backdrop  
  `hub`
- [#23](https://github.com/illycut/ai-dev/issues/23) **P1** [hub] Art accent extraction with contrast check → {url, accent, accent_is_safe}  
  `hub`
- [#24](https://github.com/illycut/ai-dev/issues/24) **P1** [hub] Service-direct high-res art resolution (Tidal CDN, YT Music thumbnails)  
  `hub`
- [#25](https://github.com/illycut/ai-dev/issues/25) **P0** [hub] Serve the PWA and self-hosted fonts from the hub over HTTPS  
  `hub, infra`
- [#26](https://github.com/illycut/ai-dev/issues/26) **P0** [app] Scaffold Next.js PWA (TypeScript, Tailwind, manifest, iOS standalone meta)  
  `app`
- [#27](https://github.com/illycut/ai-dev/issues/27) **P0** [app] Design tokens as CSS custom properties + Tailwind mapping + type scale  
  `app, design`
- [#28](https://github.com/illycut/ai-dev/issues/28) **P0** [app] Hub client: WebSocket state store with reconnect + REST commands with optimistic updates  
  `app`
- [#29](https://github.com/illycut/ai-dev/issues/29) **P0** [app] Hub-unreachable banner and command-failed toast  
  `app`
- [#30](https://github.com/illycut/ai-dev/issues/30) **P0** [app] Mini-player (persistent) with zone dot cluster  
  `app`
- [#31](https://github.com/illycut/ai-dev/issues/31) **P0** [app] NP-1/NP-2 Now Playing screen: hero art, tinted backdrop, metadata, service badge  
  `app`
- [#32](https://github.com/illycut/ai-dev/issues/32) **P0** [app] CTL-4 Scrubber with cue bubble, touch-seek, and disabled state for radio  
  `app`
- [#33](https://github.com/illycut/ai-dev/issues/33) **P0** [app] CTL-1/2/3 Transport row: prev, −15, play/pause, +15, next with 56px hit targets  
  `app`
- [#34](https://github.com/illycut/ai-dev/issues/34) **P0** [app] VOL-1/2/3 Volume sheet: per-device sliders, linked master, mute  
  `app`
- [#35](https://github.com/illycut/ai-dev/issues/35) **P0** [app] ZON-1/2/3 Zone/target picker sheet with power toggles and multi-select  
  `app`
- [#36](https://github.com/illycut/ai-dev/issues/36) **P1** [app] Mini-player expand: shared-element artwork morph, 320ms spring, reduced-motion crossfade  
  `app, design`
- [#37](https://github.com/illycut/ai-dev/issues/37) **P1** [app] Icon set: Lucide base, filled transport glyphs, ±15 numbered icons, amp/speaker glyphs  
  `app, design`
- [#38](https://github.com/illycut/ai-dev/issues/38) **P1** [spike] PWA scrubber touch feel on iOS: go/no-go on React Native  
  `app, spike`
- [#39](https://github.com/illycut/ai-dev/issues/39) **P1** [app] Loading skeletons, empty states, and offline-device rendering  
  `app`
- [#40](https://github.com/illycut/ai-dev/issues/40) **P1** [app] Accessibility pass: contrast, focus rings, touch targets, screen reader announcements  
  `app`

## Phase 3 — Tidal browse + Home

_Tidal auth + browse, play routing, play-history store, home screen, settings. Exit: browse-to-play works on each system; home populated._

- [#41](https://github.com/illycut/ai-dev/issues/41) **P0** [hub] SET-1 Tidal auth: OAuth device-code flow via tidalapi, token vault, refresh  
  `hub`
- [#42](https://github.com/illycut/ai-dev/issues/42) **P0** [hub] LIB-1/2/3 GET /api/browse/tidal/*: favorite albums, saved playlists, own playlists, tracks  
  `hub`
- [#43](https://github.com/illycut/ai-dev/issues/43) **P0** [hub] POST /api/play: content_ref → HEOS and Sonos playback by canonical id  
  `hub`
- [#44](https://github.com/illycut/ai-dev/issues/44) **P0** [hub] HOME-2 Play history store (SQLite) + GET /api/history  
  `hub`
- [#45](https://github.com/illycut/ai-dev/issues/45) **P0** [hub] HOME-1/4/5 GET /api/home aggregate  
  `hub`
- [#46](https://github.com/illycut/ai-dev/issues/46) **P1** [hub] SET-1/2/3 Settings API: account statuses, hub info, zones list, restart  
  `hub`
- [#47](https://github.com/illycut/ai-dev/issues/47) **P0** [app] HOME-2 Home screen: Recently Played rail  
  `app`
- [#48](https://github.com/illycut/ai-dev/issues/48) **P0** [app] HOME-1/3/4 Home: merged playlists grid and favorite albums grid with art cards + badges  
  `app`
- [#49](https://github.com/illycut/ai-dev/issues/49) **P0** [app] Browse detail: album / playlist track list with play-on-target  
  `app`
- [#50](https://github.com/illycut/ai-dev/issues/50) **P0** [app] HOME-2 Recents tap → target picker with last-used target pre-highlighted → play  
  `app`
- [#51](https://github.com/illycut/ai-dev/issues/51) **P0** [app] SET-1/2/3 Settings screen: Accounts, Hub, Zones  
  `app`
- [#52](https://github.com/illycut/ai-dev/issues/52) **P1** [app] 'Connect service' empty-state cards that start the hub sign-in flow  
  `app`
- [#53](https://github.com/illycut/ai-dev/issues/53) **P1** [app] SET-4 Official brand logo assets in service chips  
  `app, design`

## Phase 4 — Sync Play

_Sync engine for Tidal content, drift correction, sync UI. Exit: start delta <200ms, drift held <300ms._

- [#54](https://github.com/illycut/ai-dev/issues/54) **P0** [hub] CTL-5 Sync engine: resolve + prime (dual queue, paused at track 1, position 0)  
  `hub`
- [#55](https://github.com/illycut/ai-dev/issues/55) **P0** [hub] CTL-5 Latency measurement and offset-compensated start  
  `hub`
- [#56](https://github.com/illycut/ai-dev/issues/56) **P1** [hub] CTL-6 Drift monitor + correction loop with rate cap and track-boundary re-verify  
  `hub`
- [#57](https://github.com/illycut/ai-dev/issues/57) **P0** [hub] POST /api/sync/play, POST /api/sync/stop, sync state in WebSocket  
  `hub`
- [#58](https://github.com/illycut/ai-dev/issues/58) **P1** [hub] Sync tuning tooling: drift log CSV and threshold config  
  `hub`
- [#59](https://github.com/illycut/ai-dev/issues/59) **P0** [app] CTL-5 Sync Play button (the only amber-filled button)  
  `app`
- [#60](https://github.com/illycut/ai-dev/issues/60) **P0** [app] Sync chip: Synced / Adjusting / Sync lost with single-pulse correction feedback  
  `app`

## Phase 5 — Pandora

_Pandora station browse/play per system, merged list UI. Exit: stations playable on either system._

- [#61](https://github.com/illycut/ai-dev/issues/61) **P0** [hub] PAN-1/PAN-2 Pandora station browse per system  
  `hub`
- [#62](https://github.com/illycut/ai-dev/issues/62) **P0** [hub] Play Pandora station on a target with seekable=false  
  `hub`
- [#63](https://github.com/illycut/ai-dev/issues/63) **P0** [hub] PAN-3 Merged station list with per-station side availability  
  `hub`
- [#64](https://github.com/illycut/ai-dev/issues/64) **P0** [app] HOME-5/PAN-3 Pandora stations section and station play with device picker  
  `app`

## Phase 6 — YT Music

_YT Music Sonos-native browse/play; HEOS yt-dlp workaround spike. Exit: go/no-go on HEOS YT Music._

- [#65](https://github.com/illycut/ai-dev/issues/65) **P0** [hub] LIB-4 YouTube Music auth (ytmusicapi OAuth) and browse: library playlists, liked albums  
  `hub`
- [#66](https://github.com/illycut/ai-dev/issues/66) **P0** [hub] LIB-4 Play YouTube Music on Sonos by canonical id (SMAPI / URI construction)  
  `hub, spike`
- [#67](https://github.com/illycut/ai-dev/issues/67) **P1** [spike] LIB-5 YouTube Music on HEOS: yt-dlp stream resolution + hub-side proxy (time-boxed, 2 days)  
  `hub, spike`
- [#68](https://github.com/illycut/ai-dev/issues/68) **P0** [app] LIB-4 YouTube Music content in home grid and side-availability handling in the target picker  
  `app`

## Phase 7 — AirPlay bridge

_Scripted Pandora sync via hub Mac AirPlay 2 multi-output. Exit: one-button Pandora sync (experimental)._

- [#69](https://github.com/illycut/ai-dev/issues/69) **P2** [spike] PAN-4 AirPlay 2 multi-output scripting from the hub Mac  
  `hub, spike`
- [#70](https://github.com/illycut/ai-dev/issues/70) **P2** [hub] PAN-4 Pandora Sync mode: POST /api/pandora-sync/{start|stop} driving local playback + AirPlay outputs  
  `hub`
- [#71](https://github.com/illycut/ai-dev/issues/71) **P2** [app] PAN-4 Pandora Sync affordance behind an experimental flag  
  `app`

## Cross-cutting

_Observability, CI, tests, updates, and requirement gaps (queue, search, shuffle) not tied to one phase._

- [#72](https://github.com/illycut/ai-dev/issues/72) **P1** [hub] SET-2 Self-update: check for updates and apply (git pull + uv sync + restart)  
  `hub`
- [#73](https://github.com/illycut/ai-dev/issues/73) **P1** [hub] Observability: uptime, command latency, sync metrics for the §10 success metrics  
  `hub, infra`
- [#74](https://github.com/illycut/ai-dev/issues/74) **P1** [infra] Tailscale remote access verification and hub HTTPS hostname  
  `infra`
- [#75](https://github.com/illycut/ai-dev/issues/75) **P1** [infra] CI: lint, type-check, and tests for hub and app on PRs  
  `infra`
- [#76](https://github.com/illycut/ai-dev/issues/76) **P1** [app] Playwright E2E suite against the fake hub  
  `app`
- [#77](https://github.com/illycut/ai-dev/issues/77) **P1** [hub][app] Queue / up next view (requirement gap)  
  `hub, app`
- [#78](https://github.com/illycut/ai-dev/issues/78) **P1** [hub][app] Search across Tidal and YouTube Music (requirement gap)  
  `hub, app`
- [#79](https://github.com/illycut/ai-dev/issues/79) **P2** [hub][app] Shuffle and repeat (requirement gap)  
  `hub, app`
- [#80](https://github.com/illycut/ai-dev/issues/80) **P1** [docs] API contract document with every endpoint, the art object, error envelope, and WebSocket messages  
  `documentation`
- [#81](https://github.com/illycut/ai-dev/issues/81) **P1** [docs] Apply PRD review changes to the PRD (v1.1)  
  `documentation`
