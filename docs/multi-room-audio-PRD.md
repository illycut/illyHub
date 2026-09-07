# Product Requirements Document
## Multi-Room Audio Control App (HEOS + Sonos)

**Version:** 1.0 Final
**Owner:** James
**Date:** September 7, 2026
**Status:** Approved for build

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
- Denon power/zone control uses the legacy telnet/HTTP API on port 23 (`ZMON`/`ZMOFF`, `Z2ON`/`Z2OFF`), not the HEOS CLI, which handles power poorly.

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

### 4.5 API Contract (sketch)

**REST (commands):**
```
GET  /api/devices                      # all discovered players + zones
GET  /api/browse/{service}/{path}      # library navigation
POST /api/play                         # {target, content_ref}
POST /api/transport/{action}           # play|pause|next|prev|skip±15
POST /api/seek                         # {target, position_ms}
POST /api/volume                       # {target, level} | {linked: true, delta}
POST /api/sync/play                    # {content_ref} → dual resolution + start
POST /api/zone/power                   # {zone, on|off}
GET  /api/art/{cache_key}              # proxied artwork
```

**WebSocket (state push):**
```
{type: "state", devices: [...], now_playing: {...}, positions: {...}, sync: {...}}
```
Full-state snapshots on connect, deltas thereafter.

---

## 5. Technical Stack

| Layer | Choice | Rationale |
|-------|--------|-----------|
| Hub language | Python 3.12+ | `pyheos`, `SoCo`, `ytmusicapi` solve the three hardest integrations off the shelf |
| Hub framework | FastAPI + uvicorn | Async-native, WebSocket support, minimal boilerplate |
| HEOS | `pyheos` | Battle-tested (powers Home Assistant HEOS integration) |
| Sonos | `SoCo` | Mature, full UPnP + MusicService coverage |
| YT Music | `ytmusicapi` | Unofficial but stable library access |
| Denon power | Raw telnet (asyncio) | Trivial protocol, no dependency needed |
| Client | Next.js PWA (TypeScript/React) | Matches existing stack; served from hub or Vercel; installable |
| Process supervision | `launchd` LaunchAgent, `KeepAlive=true` | Native macOS, restarts on crash, starts on boot |
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
- [ ] `caffeinate -s` in the LaunchAgent (or Amphetamine) as sleep insurance
- [ ] Static IP or DHCP reservation for the hub
- [ ] DHCP reservations for all HEOS/Sonos/Denon devices (stable addressing survives discovery hiccups)
- [ ] Enable SSH + Screen Sharing (headless administration)
- [ ] Install Tailscale; verify client reachability
- [ ] Auto-login enabled (required for LaunchAgent + AirPlay bridge in user session)
- [ ] Confirm AirPlay 2 multi-output works manually before scripting it (Music app → output to both systems)

### 6.3 Service Deployment
- [ ] Python via `uv` or pyenv; project virtualenv
- [ ] LaunchAgent plist: `KeepAlive`, `RunAtLoad`, stdout/stderr to rotating logs
- [ ] Log rotation (`newsyslog` or app-level)
- [ ] Health endpoint (`GET /api/health`) + optional uptime ping

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
