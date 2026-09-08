# Product Requirements Document
## Multi-Room Audio Control App (HEOS + Sonos)

**Version:** 1.2
**Owner:** James
**Date:** September 8, 2026
**Status:** Phases 0-6 built; hub running on the LAN

---

## Revision history

| Version | Date | Change |
|---------|------|--------|
| 1.0 | Sept 7, 2026 | Approved for build. |
| 1.1 | — | Never issued. The `docs/prd-review.md` changes were adopted in the build plan and the backlog but not folded back into this document; 1.2 does that. |
| 1.2 | Sept 8, 2026 | Corrects this document against what the hardware actually does. The hub now runs on the LAN against a Denon AVR-X3400H and a Sonos Amp, and three architectural claims here did not survive that: the Denon HTTP control API, HEOS volume on an AVR, and the LaunchAgent deployment model. Also folds in the adopted review items. Every change is marked **[1.2]** in place, and `ops/RUNBOOK.md` section 5 holds the raw verification log. |

**How to read this document.** Requirement IDs are stable; do not renumber them. Where hardware
contradicted an earlier assumption, the original claim is struck rather than deleted, so a
reader can see what was believed and why it changed.

---

## 1. Product Overview

### 1.1 Summary
A standalone app that provides unified control of a mixed HEOS (Denon/Marantz) and Sonos audio environment from a single touch interface. The system consists of two components: a **local hub service** running on an always-on Mac that speaks both native protocols, and a **touch UI client** that talks only to the hub.

### 1.2 Problem Statement
HEOS and Sonos are closed ecosystems with separate apps, separate service logins, and no native interoperability. Controlling both means juggling two apps, and playing the same music on both systems simultaneously is not supported by either vendor. This app unifies control and delivers best-effort cross-system synchronized playback.

### 1.3 Goals
1. One interface for browsing, playback, volume, and zone control across both ecosystems
2. "Sync Play" — same content, same moment, on both systems (best-effort, not sample-accurate)
3. Full library access for Tidal and YouTube Music
4. Pandora station playback (per-system, with a documented AirPlay 2 sync path)
5. Reliable, always-on operation with no cloud dependency for core control

### 1.4 Non-Goals (v1)
- Sample-accurate cross-brand audio sync (physically impossible without a shared clock)
- Native Pandora sync via API orchestration (platform limitation; AirPlay 2 is the workaround)
- Support for ecosystems beyond HEOS and Sonos
- Multi-home / remote-first operation (Tailscale covers remote access as a bonus, not a requirement)
- App Store distribution

---

## 2. Users & Environment

### 2.1 Primary User
Household members controlling music playback. Technical setup and maintenance handled by owner.

### 2.2 Target Environment
- **HEOS side:** Denon/Marantz amplifier(s) with HEOS built-in
- **Sonos side:** One or more Sonos players
- **Hub hardware:** 2019 16" MacBook Pro (Intel i9 2.4GHz), macOS Sequoia 15.1.1, always-on, wired Ethernet, closed-lid
- **Network:** Single flat LAN (multicast/SSDP must reach all devices and the hub)
- **Client devices:** Phones/tablets on the same LAN (or via Tailscale)

---

## 3. Functional Requirements

### 3.1 Music Library Access

| ID | Requirement | Priority |
|----|-------------|----------|
| LIB-1 | Browse and play favorite albums from Tidal on both systems | P0 |
| LIB-2 | Browse and play saved playlists from Tidal on both systems | P0 |
| LIB-3 | Browse and play personal (user-created) playlists from Tidal on both systems | P0 |
| LIB-4 | Browse and play YouTube Music favorites/playlists on Sonos (native) | P0 |
| LIB-5 | YouTube Music playback on HEOS via URL-resolution workaround | P1 |
| LIB-6 | Browse and select Pandora stations per-system | P0 |

**Notes:**
- Tidal is natively supported by both ecosystems. HEOS: `heos://browse/browse` after `system/sign_in`. Sonos: MusicService browse via SoCo.
- YouTube Music is native on Sonos only. HEOS has no YT Music integration. The P1 workaround resolves tracks via `ytmusicapi` and streams by URL to HEOS. Decision point: if the workaround proves unstable, YT Music ships as Sonos-only.
- Content matching for Sync Play keys on Tidal track IDs (canonical per service) — this is the best-case sync scenario.

### 3.2 Playback Controls

| ID | Requirement | Priority |
|----|-------------|----------|
| CTL-1 | Play / pause per system and globally | P0 |
| CTL-2 | Previous / next track | P0 |
| CTL-3 | ±15 second skip (relative seek) | P0 |
| CTL-4 | Touch-seek on progress timeline with cue (scrub to position, seek on release) | P0 |
| CTL-5 | Sync Play: initiate same album/playlist on both systems simultaneously | P0 |
| CTL-6 | Drift correction: monitor position on both systems during Sync Play, nudge laggard via seek when drift exceeds threshold (~300ms) | P1 |

**Notes:**
- Seek (CTL-3, CTL-4) applies to on-demand content only. Radio-style streams (Pandora) do not support seek; UI must disable seek affordances for those sources.
- Sync Play sets expectations in UI copy: "close, not perfect." Target: start within ~200ms, hold drift under 300ms via correction.
- HEOS transport: `player/set_play_state`, `play_previous`, `play_next`, `player_now_playing_progress` events. Sonos transport: AVTransport Play/Pause/Previous/Next/Seek, GetPositionInfo polled ~1/sec.

### 3.3 Volume

| ID | Requirement | Priority |
|----|-------------|----------|
| VOL-1 | Per-device volume slider (HEOS and each Sonos player) | P0 |
| VOL-2 | Linked "both" master slider moving all volumes proportionally | P1 |
| VOL-3 | Mute toggle per device | P1 |

**Notes [1.2]:**
- **An AVR-hosted HEOS player has no volume of its own.** `heos://player/set_volume` returns
  `success` and applies nothing, and `get_volume` reports `0` regardless of the receiver's real
  `MV` (verified on an AVR-X3400H, firmware 3.139.173). VOL-1 and VOL-3 for that player route to
  the amplifier zone over the Denon link, and the zone's level mirrors onto the player so the UI
  has one truthful number. A hub that trusted the HEOS values would show a dead slider.
- **An amplifier zone is a volume target in its own right.** A zone driving an external amp owns
  no player, so `POST /api/volume` and `/api/mute` accept a zone id as well as a player or side.
- **Not every zone can be attenuated.** A zone wired as a fixed pre-out to an external amp
  reports `supports_volume: false` / `supports_mute: false`, and the hub refuses the command
  (`unsupported_action`) rather than sending one the hardware ignores. The client must hide the
  slider on those zones. Zone 2 in the owner's install is one; the HEOS app likewise offers it
  as power-only.
- **The hub scale is coarser than the receiver's.** Hub `0-100` maps onto `MV 0-HUB_DENON_MAX_VOLUME`
  (default 60, so 61 steps), capped deliberately because `MV98` is +18 dB. A slider will settle a
  point or two from where it was dropped; VOL-2 records the settled level, not the request, so
  quantisation is not mistaken for someone turning the knob.

### 3.4 Now Playing

| ID | Requirement | Priority |
|----|-------------|----------|
| NP-1 | Album key art for active music, per system | P0 |
| NP-2 | Track title, artist, album, source badge (Tidal/YT Music/Pandora) | P0 |
| NP-3 | Live progress timeline (event-driven on HEOS, 1Hz poll on Sonos) | P0 |

**Notes:**
- HEOS art via `get_now_playing_media` → `image_url`. Sonos via `albumArtURI`.
- Device-reported art varies in resolution (HEOS especially). The hub prefers service-direct art: Tidal CDN serves arbitrary resolutions keyed on album ID; `ytmusicapi` returns multi-size thumbnails. Device URL is the fallback.
- Hub art proxy caches and serves a consistent high-resolution image (target 640px+ for cards, 1080px+ for now-playing hero) regardless of source system.
- Hub proxies all artwork to avoid CORS/mixed-content issues in a web client.

### 3.4a Home Screen & Browse Experience

| ID | Requirement | Priority |
|----|-------------|----------|
| HOME-1 | Home screen leads with playlists, merged across Tidal and YouTube Music, art-forward card grid | P0 |
| HOME-2 | "Recently Played" rail: albums, playlists, and stations played through the app, most recent first. Tap opens the target picker (last-used target pre-highlighted for speed), then plays | P0 |
| HOME-3 | Service badge on each card (Tidal / YT Music / Pandora) so cross-platform origin is visible at a glance | P1 |
| HOME-4 | Favorite albums grid, art-forward | P0 |
| HOME-5 | Pandora stations section with station art | P1 |

**Notes:**
- Neither HEOS nor Sonos exposes listening history. The hub maintains its own play-history store (SQLite), logging every play it routes: content ref, service, target system, timestamp, art cache key. Recents are hub-native; plays initiated from vendor apps appear only as now-playing state.
- Merged playlist view unions Tidal and YT Music playlists into one scrollable set, sorted by recency of play (hub history) then alphabetically. Duplicate names across services are not deduped; the badge disambiguates.

### 3.5 Pandora

| ID | Requirement | Priority |
|----|-------------|----------|
| PAN-1 | Browse and play Pandora stations natively on HEOS | P0 |
| PAN-2 | Browse and play Pandora stations natively on Sonos | P0 |
| PAN-3 | UI presents merged station list with device picker (or two lists) | P0 |
| PAN-4 | "Pandora Sync" via hub-as-AirPlay-2-sender: hub Mac plays Pandora locally and outputs to an AirPlay 2 multi-output group targeting both systems | P2 |

**Notes:**
- Native cross-system Pandora sync is impossible: each device session gets its own server-decided track sequence, and no API exists to request a specific track. Accepted limitation.
- Account constraint: Pandora typically allows one active stream per account; two native sessions may conflict.
- PAN-4 exploits the hub Mac's AirPlay 2 sender capability (Sequoia supports multi-output). This converts the manual phone-based AirPlay handoff into an in-app button. Scripting via AppleScript/Shortcuts; treat as experimental.

### 3.6 Zones & Power

| ID | Requirement | Priority |
|----|-------------|----------|
| ZON-1 | Turn Denon zones on/off (main zone, Zone 2) | P0 |
| ZON-2 | Sonos "off" = stop playback + clear group (Sonos has no power-off concept) | P0 |
| ZON-3 | Zone status display (on/off/playing) | P1 |

**Notes:**
- Denon power/zone control uses the legacy telnet API on port 23 (`ZMON`/`ZMOFF`, `Z2ON`/`Z2OFF`), not the HEOS CLI, which handles power poorly.

**Notes [1.2] — what the hardware forced:**
- **The HTTP form API is gone on current firmware.** `/goform/formiPhoneAppDirect.xml`,
  `formMainZone_MainZoneXml.xml`, `Deviceinfo.xml` and `AppCommand.xml` all return **403** on an
  AVR-X3400H running 3.139.173 (it serves nginx on 80/443 and redirects 80 to 443). Telnet is the
  only control path there. This reverses `docs/prd-review.md` 2.3, which recommended HTTP for
  commands and telnet only for event streaming; the hub now probes HTTP at connect and uses it
  only if the receiver actually answers, so older models still work.
- **Zone power has cross-zone side effects.** `Z2OFF` dropped the whole unit to `PWSTANDBY` and
  took the main zone with it, when main was the only other zone on. Every power command reads
  `PW?`/`ZM?`/`Z2?` back instead of writing an optimistic value; ZON-3 status must come from that
  read-back.
- **Telnet is single-client**, so the hub holds one connection for both commands and the status
  stream. A vendor app or a Home Assistant holding port 23 locks the hub out; that surfaces as a
  refused connection and is retried with backoff.
- **ZON-1 is per-zone, not per-player.** A zone with a fixed pre-out is power-only (see 3.3).

---

### 3.7 Settings & Accounts

| ID | Requirement | Priority |
|----|-------------|----------|
| SET-1 | Accounts area: link/unlink Tidal, YouTube Music, Pandora, and HEOS account; show connection status | P0 |
| SET-2 | Hub status: address, version, uptime; restart hub service; check for updates | P1 |
| SET-3 | Zones list: discovered hardware with vendor and zone detail | P1 |
| SET-4 | Service identity throughout the app uses official platform logo assets (per brand kit usage terms), not text labels | P1 |

---

## 4. System Architecture

### 4.1 Component Diagram

```
┌─────────────────────────────┐
│   Touch UI Client (PWA /    │
│   React Native)             │
└───────────┬─────────────────┘
            │ WebSocket (state push) + REST (commands)
┌───────────▼─────────────────┐
│   Hub Service (Python /     │
│   FastAPI on MacBook Pro)   │
│                             │
│  ┌────────┐  ┌───────────┐  │
│  │ State  │  │ Sync      │  │
│  │ Model  │  │ Engine    │  │
│  └────────┘  └───────────┘  │
│  ┌────────┐  ┌───────────┐  │
│  │ Art    │  │ AirPlay   │  │
│  │ Proxy  │  │ Bridge    │  │
│  └────────┘  └───────────┘  │
└──┬────────┬────────┬────────┘
   │        │        │
   │ TCP    │ UPnP/  │ Telnet
   │ 1255   │ SOAP   │ 23
   │        │ 1400   │
┌──▼───┐ ┌──▼────┐ ┌─▼─────┐
│ HEOS │ │ Sonos │ │ Denon │
│ CLI  │ │ UPnP  │ │ Legacy│
└──────┘ └───────┘ └───────┘
```

### 4.2 Why Hub + Client (not direct app-to-device)
1. HEOS behaves best with a single long-lived TCP socket; multiple clients connecting directly cause contention
2. Sonos UPnP eventing requires a callback HTTP listener — painful/impossible from a mobile app
3. Hub normalizes two protocols into one state model; UI stays dumb and fast
4. Push-based state via WebSocket: every client sees changes instantly regardless of origin (including changes made from vendor apps)

### 4.3 Hub Responsibilities
- **Connection manager:** persistent HEOS socket (with 30–60s keepalive), Sonos UPnP subscriptions, Denon telnet session; automatic reconnect with backoff
- **Discovery:** SSDP for both `urn:schemas-denon-com:device:ACT-Denon:1` and `urn:schemas-upnp-org:device:ZonePlayer:1`; devices pinned by IP reservation
- **State model:** unified representation of players, zones, now-playing, volume, position, source
- **Command router:** translates normalized commands to protocol-specific calls
- **Sync engine:** content resolution (Tidal ID matching), dual-queue, offset-compensated start, drift monitor + correction
- **Auth vault:** HEOS account sign-in, Sonos service linkage, `ytmusicapi` OAuth, stored locally
- **Art proxy:** fetches and caches album art, preferring service-direct high-res sources; serves over hub origin
- **Play history store:** SQLite log of every routed play (content, service, target, timestamp) powering the Recently Played rail
- **AirPlay bridge (P2):** drives local Pandora playback + AirPlay 2 multi-output via AppleScript/Shortcuts

### 4.4 Client
- Single-page touch UI: zone list, now playing (art, timeline, transport), volume, browse (Tidal/YT Music/Pandora), Sync Play button
- Connects to hub WebSocket for state, REST for commands
- Distribution: PWA served by the hub itself (add to home screen) — zero app-store friction. React Native as a later option if native scrub feel demands it.

### 4.5 API Contract

**[1.2] `docs/api.md` is the contract.** It is generated against the built hub and covers every
endpoint, the ack and error envelopes, the art object and the WebSocket messages. The live
OpenAPI document is at `/openapi.json` on the running hub. The sketch below is kept only as the
shape of the thing; where the two disagree, `docs/api.md` wins.

```
GET  /api/devices                      # all discovered players + zones
GET  /api/browse/{service}/{path}      # library navigation
POST /api/play                         # {target, content_ref}
POST /api/transport/{action}           # play|pause|next|prev|skip±15
POST /api/seek                         # {target, position_ms}
POST /api/volume                       # {target, level} | {linked: true, delta}
POST /api/mute                         # {target, muted}
POST /api/sync/play                    # {content_ref} → dual resolution + start
POST /api/zone/power                   # {zone_id, on}
GET  /api/art/{cache_key}              # proxied artwork
```

**[1.2] Contract change for the client:** `POST /api/volume` and `POST /api/mute` now accept an
**amplifier zone id** as `target`, alongside a player id, a side id and `"all"`. This exists
because a zone driving an external amp owns no player and was otherwise unaddressable. `Zone`
gained `volume`, `muted`, `supports_volume` and `supports_mute`; a client must read those flags
and hide the control rather than offer one the hardware ignores. See 3.3 and `docs/api.md`
"Concepts" and "Command endpoints".

**WebSocket (state push):**
```
{type: "snapshot", state: {...}}   then   {type: "delta", ...}
```
Full-state snapshot on connect, deltas thereafter, plus `ack` messages carrying command outcomes.

---

## 5. Technical Stack

| Layer | Choice | Rationale |
|-------|--------|-----------|
| Hub language | Python 3.12+ | `pyheos`, `SoCo`, `ytmusicapi` solve the three hardest integrations off the shelf |
| Hub framework | FastAPI + uvicorn | Async-native, WebSocket support, minimal boilerplate |
| HEOS | `pyheos` | Battle-tested (powers Home Assistant HEOS integration) |
| Sonos | `SoCo` | Mature, full UPnP + MusicService coverage |
| YT Music | `ytmusicapi` | Unofficial but stable library access |
| Denon power **and volume** | Raw telnet (asyncio), port 23 | **[1.2]** Trivial protocol, no dependency. Now the primary control path: current firmware 403s the HTTP form API, and HEOS cannot set volume on an AVR (see 3.3, 3.6) |
| Client | Next.js PWA (TypeScript/React) | Matches existing stack; served from hub or Vercel; installable. **[1.2]** Needs Node 20+ to build (`next` 16, `react` 19); `app/package.json` declares `engines.node >=20.9` with `engine-strict` so a stale Node fails loudly |
| Process supervision | **`launchd` LaunchDaemon (root, system context)**, `KeepAlive=true` | **[1.2]** Was a user LaunchAgent. macOS Sequoia's Local Network gate denies LAN access to an agent, and to a daemon with `UserName` set, with no way to grant it; only the system context reaches the LAN. Upside: starts at boot with no login, so FileVault stays on (see 6.2) |
| Remote access | Tailscale | Zero port-forwarding, secure by default |

---

## 6. Hub Setup Specification (MacBook Pro)

### 6.1 Hardware Prep
- [ ] Wired Ethernet connection (USB-C adapter if needed) — required for reliable multicast/SSDP and UPnP eventing
- [ ] Power connected permanently (enables closed-lid operation)
- [ ] Shelf placement with airflow; lid closed
- [ ] Battery health check on a recurring basis (aging Intel MBP; watch for swelling)

### 6.2 macOS Configuration
- [ ] System Settings → Energy: prevent sleep when display off; start up automatically after power failure; wake for network access
- [ ] `caffeinate -s` in the LaunchDaemon (or Amphetamine) as sleep insurance
- [ ] Static IP or DHCP reservation for the hub
- [ ] DHCP reservations for all HEOS/Sonos/Denon devices (stable addressing survives discovery hiccups)
- [ ] Enable SSH + Screen Sharing (headless administration)
- [ ] Install Tailscale; verify client reachability
- [x] ~~Auto-login enabled (required for LaunchAgent + AirPlay bridge in user session)~~
      **[1.2] Not needed, and FileVault can stay ON.** The hub is a root LaunchDaemon and starts
      at boot with nobody logged in. This retires the "FileVault and auto-login are mutually
      exclusive" trade-off in `docs/prd-review.md` 5 for everything except the Phase 7 AirPlay
      bridge, which does need a user session and should carry its own helper rather than
      weakening disk encryption for the whole machine.
- [ ] Confirm AirPlay 2 multi-output works manually before scripting it (Music app → output to both systems)
- [ ] **[1.2]** Clone the repo somewhere a launchd job can read it: **not** `~/Documents`,
      `~/Desktop` or `~/Downloads`. Those are TCC-protected, a daemon has no consent grant, and
      the hub then hangs with nothing in any log. `~/illyHub` is fine; `ops/install.sh` refuses
      the protected paths.

### 6.3 Service Deployment
- [ ] Python via `uv`; project virtualenv. `uv` installs without Homebrew:
      `curl -LsSf https://astral.sh/uv/install.sh | sh`
- [ ] **[1.2] LaunchDaemon** plist in `/Library/LaunchDaemons`, running as root:
      `KeepAlive`, `RunAtLoad`, stdout/stderr to rotating logs. `ops/install.sh` installs it and
      retires any older user agent. A user LaunchAgent cannot reach the LAN (see 5).
- [ ] **[1.2] `HUB_DATA_DIR` must be an absolute path outside the repo** (`/usr/local/var/illyhub`,
      mode 0700, `vault.key` 0600). As root the hub would otherwise leave root-owned files through
      the checkout and break running the hub or the tests as yourself.
- [ ] Log rotation (`newsyslog` or app-level)
- [ ] Health endpoint (`GET /api/health`) + optional uptime ping
- [ ] **[1.2] Intel-Mac dependency constraints** (see `ops/RUNBOOK.md` 3b): `cryptography` is
      pinned `<43` because 43+ publishes arm64-only macOS wheels and the hub Mac is Intel;
      Node 20+ is required to build the PWA.

### 6.4 OS Lifecycle Note
Intel Macs end OS support after macOS Tahoe, with security updates for a few years beyond — roughly end-of-decade safe service life for a LAN-only hub behind Tailscale. Plan eventual migration to a Mac mini; the hub is a portable Python service, so migration is copy + relink LaunchAgent.

---

## 7. Sync Engine Design (CTL-5/6 detail)

1. **Resolve:** given content_ref (Tidal album/playlist ID), resolve to HEOS-playable and Sonos-playable references. Tidal-to-Tidal: IDs are canonical, direct match. YT Music: Sonos-native ref vs. HEOS URL-stream ref.
2. **Prime:** load queues on both systems, paused at track 1, position 0.
3. **Measure:** per-system command latency estimated from rolling average of recent command round-trips.
4. **Fire:** issue play to the slower system first, offset by measured latency delta.
5. **Monitor:** compare position reports (HEOS events, Sonos 1Hz poll). Compute drift.
6. **Correct:** if |drift| > 300ms for 2+ consecutive samples, seek the laggard to leader position + lookahead. Cap corrections at 1 per 10s to avoid audible stutter loops.
7. **Track boundaries:** on track change, re-verify both systems advanced to the same track; re-prime if diverged.

**Known limits:** correction granularity is bounded by seek precision (~100–500ms on these platforms). Same-room listening will exhibit audible artifacts; positioning in UI as multi-room feature. Long-session clock drift is expected and handled by the monitor loop.

**[1.2] Measured on the hardware.** Three numbers from the first LAN run bear directly on steps
3 to 6, all in `ops/RUNBOOK.md` 5:

- **Position reports are whole-second and drift against the wall clock.** A Sonos Amp held
  `1:33:01` for two consecutive samples, then moved `1:33:04 → 1:33:06`. This confirms
  `docs/prd-review.md` 1.2: the 300 ms threshold in step 6 cannot be read off raw position
  deltas. The hub timestamps tick edges and interpolates, and `Position.confidence` reports how
  tight the estimate is; step 5 must compare interpolated estimates, never two reported seconds.
- **Command acknowledgement is not a usable clock.** A play took 1.7-1.9 s to confirm in hub
  state while a pause took 0.21 s, because both vendors emit a transitional state for about a
  second while a stream buffers (Sonos `TRANSITIONING`, HEOS `state=unknown`). Step 3's latency
  estimate must come from position observations, not from when a state change lands.
- **Seek works on a YouTube Music HLS stream** (`x-sonosapi-hls-static`, `sid=284`) and restored
  an exact position, so step 6's correction mechanism is available on YT Music content on the
  Sonos side, not only on Tidal.

---

## 7a. Design & UX Direction

**Visual language:** Spotify/Sonos-class display. Dark UI, artwork carries the interface. Chrome stays minimal so album art does the visual work.

**Home screen (the app's front door):**
1. **Recently Played rail** at top: horizontal scroll of large art cards, resume-on-tap
2. **Your Playlists** grid: merged Tidal + YT Music, art-forward square cards with service badge
3. **Favorite Albums** grid below
4. **Pandora Stations** section with station art
5. Persistent mini-player bar pinned to bottom (art thumbnail, title, play/pause, target indicator), expands to full Now Playing

**Now Playing screen:**
- Hero artwork dominating the upper two-thirds, subtle blurred-art background fill
- Title/artist/album with service badge
- Touch-scrubbing timeline with cue bubble showing seek target time
- Transport row: prev, minus 15s, play/pause, plus 15s, next
- Volume: per-device sliders in an expandable sheet, linked master slider on the main screen
- Sync Play state chip when active (synced / drifting / correcting)

**Zone/target picker:** persistent, one tap from anywhere. Playing content always shows which system(s) it targets.

**Interaction principles:**
- Every browsable item is art-first; text is secondary
- Zero modal dialogs for playback actions; optimistic UI with WebSocket-confirmed state
- Scrub gesture: drag anywhere on timeline, release to commit, with haptic tick at track start/end where the platform supports it

---

## 8. Build Phases

| Phase | Scope | Exit criteria |
|-------|-------|---------------|
| **0. Foundation** | Hub scaffold, discovery, persistent connections, state model, health endpoint | Both systems discovered; state visible via API |
| **1. Core control** | Volume, play/pause, prev/next, zone power (telnet), WebSocket push | Full transport + volume from curl/basic UI |
| **2. Now Playing UI** | PWA shell, art proxy (service-direct high-res), timeline, touch seek, ±15s, mini-player | Daily-drivable for basic control |
| **3. Tidal browse + Home** | Sign-in flows, favorites/playlists browse on both, play routing, play-history store, home screen (recents rail + merged playlist grid) | Browse-to-play works on each system; home screen populated |
| **4. Sync Play** | Sync engine for Tidal content, drift correction, UI affordance | Two-system start <200ms apart, drift held <300ms |
| **5. Pandora** | Station browse/play per system, merged list UI | Stations playable on either system |
| **6. YT Music** | Sonos-native browse/play; HEOS URL workaround spike | Go/no-go decision on HEOS YT Music |
| **7. AirPlay bridge** | Scripted Pandora sync mode via hub Mac | One-button Pandora sync (experimental) |

Phases 0–2 are the de-risking core: all-native APIs, no service auth complexity. Everything after is additive.

**[1.2] Status.** Phases 0-6 are built and their tests pass. The hub runs on the LAN as a root
LaunchDaemon and sees the real HEOS, Sonos and Denon devices. What is verified against hardware
versus still only against the protocol fakes is tracked in `ops/RUNBOOK.md` 5, not here, so the
list stays current. Verified so far: discovery, Sonos transport and seek, hub state following a
device change over UPnP events, Denon zone power, and per-zone volume and mute end to end
through REST. Not yet exercised on hardware: Sync Play (Phase 4), Pandora browse and play
(Phase 5), YouTube Music (Phase 6), and the two Phase 0 gates ai-dev #9 and #10. The PWA is not
built on the hub Mac yet (`/api/health` reports `static.app: false`), so the API is running
without a UI in front of it.

---

## 9. Risks & Mitigations

| Risk | Likelihood | Impact | Mitigation |
|------|------------|--------|------------|
| Sonos deprecates local UPnP API | Low-med | High | SoCo community tracks changes; official cloud API as fallback (adds latency + OAuth) |
| `ytmusicapi` breaks (unofficial) | Medium | Medium | YT Music scoped as Sonos-native P0, HEOS workaround P1; degradable |
| Sync drift worse than 300ms in practice | Medium | Medium | Tunable thresholds; UI framed as best-effort; multi-room positioning |
| HEOS firmware changes CLI behavior | Low | Medium | Pin firmware; pyheos tracks protocol versions |
| Pandora single-stream account limit | Medium | Low | Documented limitation; AirPlay bridge sidesteps (one stream) |
| MacBook battery swelling (age) | Medium | Medium | Periodic physical check; battery pull as option |
| AppleScript AirPlay automation fragility | High | Low | P2/experimental; manual AirPlay flow always available |
| Multicast blocked by network gear | Low | High | Flat LAN requirement documented; IGMP snooping config note |
| **[1.2]** macOS gates LAN access from background processes | **Realised** | High | Sequoia's Local Network permission denies a user LaunchAgent and a `UserName` daemon with no way to grant it. Mitigation in place: root LaunchDaemon in the system context. A future macOS could close that too, in which case the hub needs a logged-in session or a signed, notarised app bundle |
| **[1.2]** Tidal concurrent-stream limit breaks Sync Play | Unknown | High | The gap `docs/prd-review.md` 1.1 flagged, still untested (ai-dev #9). Note the accounts differ per ecosystem in this install (HEOS Tidal is one account; Sonos was playing YouTube Music), so the limit may not bite here. Fallback remains a second Tidal account, one per ecosystem |
| **[1.2]** Vendor firmware removes a local control API | **Realised** | Med | The Denon HTTP form API now 403s on current firmware; telnet still works and is the primary path. The hub probes at connect and degrades rather than assuming. Same class of risk as the HEOS CLI row above, but already realised once |
| **[1.2]** Dependency wheels drop Intel macOS | **Realised** | Med | `cryptography` 43+ is arm64-only on macOS and broke `uv sync` on the Intel hub Mac. Pinned `<43`. Expect this to recur as more wheels go arm64-only; the long-term fix is the Mac mini migration in 6.4 |

---

## 10. Success Metrics

- Hub uptime ≥ 99% over 30 days (health-check log)
- Command-to-audible-response < 500ms on LAN for transport/volume
- Sync Play: start delta < 200ms, sustained drift < 300ms on Tidal content
- Household actually stops opening the vendor apps for daily control

---

## 11. Resolved Decisions

1. **YT Music on HEOS:** attempt the URL-resolution workaround, held at P1 with a go/no-go spike in Phase 6. Sonos-native YT Music ships regardless.
2. **Client platform:** PWA served from the hub. React Native revisited only if scrubber touch feel disappoints in Phase 2.
3. **Transport layout:** full 5-button row (prev, −15, play/pause, +15, next) with 56px hit targets per the design system.
4. **Recents behavior:** tapping a Recently Played card always opens the target picker before playing, with the last-used target pre-highlighted so the common case stays two fast taps.
5. **Art cache:** disk-backed with 30-day TTL, keyed by service + content ID; pre-blurred backdrop variants cached alongside.
6. **Multi-user auth:** none in v1. Trusted LAN plus Tailscale is the security boundary.
7. **[1.2] Deployment model:** root LaunchDaemon in the system context. Not a preference — it is
   the only context macOS Sequoia grants LAN access to (see 5). FileVault stays on and auto-login
   is retired.
8. **[1.2] Amplifier zones own their volume.** Where a HEOS player is hosted by an AVR, the
   amplifier zone is the authority for level and mute and the zone value mirrors onto the player.
   HEOS's own volume surface is not trustworthy on such a player.
9. **[1.2] Zone volume capability is declared, not assumed.** A zone wired as a fixed pre-out
   reports `supports_volume: false` and the hub refuses the command. The client hides the control.
   Matching the vendor app's own affordances is the deciding test: the HEOS app offers the
   owner's Zone 2 as power-only.
10. **[1.2] Credential vault stays a Fernet file, not macOS Keychain.** Keychain was considered to
    drop the `cryptography` dependency, but the login keychain is locked when nobody is logged in
    — which is the hub's normal state — and the tests plus Linux CI cannot use Keychain, so a
    second backend would be needed anyway. The Fernet key lives beside the ciphertext, so
    directory permissions (0700/0600) do the real work, with FileVault underneath.
