# PRD Review — Multi-Room Audio Control App

**Reviewed:** multi-room-audio-PRD.md v1.0, multi-room-audio-design-system.md v1.0
**Date:** September 7, 2026
**Verdict:** Sound architecture and a well-scoped v1. Two unverified assumptions could invalidate the flagship Sync Play feature and should be tested manually before Phase 1. Several integration claims are optimistic and need spikes. Six requirement gaps noted.

---

## 1. Blocking risks (test before building the sync engine)

### 1.1 Tidal concurrent-stream limit is not in the risk table
Tidal enforces one active stream per account: starting playback on a second device pauses the first. Sync Play (CTL-5) requires two simultaneous Tidal streams from the same account, one via HEOS and one via Sonos. If enforcement applies across these two integrations, Sync Play on Tidal content does not work at all.

The PRD lists this constraint for Pandora (§3.5) but not for Tidal, where it matters far more.

**Action:** 10-minute manual test in Phase 0. Start the same Tidal album on HEOS from the HEOS app and on Sonos from the Sonos app. If one pauses, the mitigation is a second Tidal account (family plan) linked to one ecosystem, and the sync engine must know which account each side uses. Same check for YouTube Music Premium, which also limits concurrent streams.

### 1.2 Sub-second drift detection from 1-second position reports
Sonos `GetPositionInfo` returns `RelTime` at whole-second resolution. HEOS `player_now_playing_progress` events arrive roughly once per second. The PRD's 300ms drift threshold cannot be measured directly from either. The sync engine must timestamp the moment each side's reported second ticks over and interpolate; this is doable but must be designed in, not discovered in Phase 4.

Also, a Sonos seek triggers a buffer re-fetch (hundreds of ms to a couple of seconds of silence). The lookahead in step 6 of §7 must be measured per device, not assumed.

---

## 2. Integration claims that need spikes

### 2.1 SoCo MusicService (SMAPI) is the weakest part of SoCo
§5 calls SoCo "mature, full UPnP + MusicService coverage." The UPnP half is true. The MusicService half is the fragile part: Sonos moved most services (Tidal and YouTube Music included) to OAuth "appLink" authentication, and SoCo's SMAPI browsing has broken repeatedly as a result. Treating Tidal browse-on-Sonos and YouTube Music browse-on-Sonos as P0 with no spike is the biggest schedule risk in the plan.

**Recommendation:** browse once, play anywhere. Use the Tidal API directly (`tidalapi`, OAuth device-code flow) and `ytmusicapi` as the single browse source for each service. Play on each device by constructing the device's playable reference from the service's canonical ID:
- Sonos: `x-sonos-http:track/{tidal_id}.flac?sid=174&...` plus DIDL-Lite metadata (well-trodden pattern in the Home Assistant / SoCo community).
- HEOS: `browse/add_to_queue` with the Tidal service `sid` and the container/media IDs, which map to Tidal IDs.

This removes two flaky browse paths, gives one auth flow per service instead of per service-per-ecosystem, and makes the sync engine's "Tidal ID is canonical" assumption a first-class contract. It does require a Phase 0 spike proving the ID-to-ref mapping works on both sides.

### 2.2 YouTube Music on HEOS is more than "URL resolution"
`ytmusicapi` returns metadata, not stream URLs. Getting a stream requires `yt-dlp`, which yields expiring, IP-bound, usually adaptive (DASH) URLs. HEOS `play_stream` wants a plain HTTP audio stream that survives the length of an album. Realistically the hub must resolve, proxy, and possibly remux per track, and this sits against YouTube's terms of service. Keep it P1, but the Phase 6 spike scope should say "yt-dlp plus hub-side stream proxy," and it should be time-boxed.

### 2.3 Denon telnet accepts one connection at a time
Port 23 on Denon/Marantz receivers allows a single client. If the HEOS app, a Home Assistant instance, or a stale hub session holds it, power commands fail silently. The HTTP form endpoint (`/goform/formiPhoneAppDirect.xml?ZMON`) is connectionless and works for commands; keep telnet only for event streaming. Add reconnect and "connection busy" handling either way.

---

## 3. Client platform: PWA constraints the PRD does not mention

Decision 2 (PWA first) is right, but three facts should be in the PRD:

1. **Service workers require a secure context.** An `http://192.168.x.x` origin gets no service worker, so no offline shell, no install prompt on Android, and degraded "Add to Home Screen" behavior. Fixes: serve via `tailscale serve` (free HTTPS with a MagicDNS hostname, works on LAN for tailnet devices), or a local CA with `mkcert` installed on each client. This is a Phase 2 setup task, not a nice-to-have.
2. **No haptics on iOS Safari.** The Vibration API is unsupported. The "haptic tick at track start/end" in §7a is React Native only; the PWA should drop it rather than ship a no-op.
3. **iOS standalone mode** works via the `apple-mobile-web-app-capable` meta and manifest even without a service worker, so the phone experience is still installable. Worth stating so the HTTPS work is understood as "better," not "required to launch."

---

## 4. Requirement gaps

| Gap | Why it matters | Suggested priority |
|-----|----------------|-------------------|
| Native grouping within each ecosystem | Multiple Sonos players must be grouped natively (sample-accurate) and the hub should target the group coordinator. Same for HEOS groups. The sync engine syncs two *sides*, not N players. Nowhere stated. | P0, Phase 1 |
| Queue / up next | Every reference app (Spotify, Sonos) exposes the queue. Users will look for it after Play. | P1 |
| Search | Home is favorites-only by design, but "play that one album I have not favorited" is a daily case. | P1 |
| Shuffle / repeat | Expected on album and playlist playback. Trivial on both protocols. | P2 |
| `/api/history`, `/api/auth/*`, `/api/health`, `/api/sync/stop`, mute, zone status | Referenced in prose (§3.7, §6.3, §3.4a) but absent from the §4.5 contract. | Contract fix |
| Art payload schema | Design system §11 specifies `{url, accent, accent_is_safe}`; PRD §4.5 only defines the image endpoint. The accent has to ride with now-playing and browse payloads. | Contract fix |

---

## 5. Hub setup details to add to §6

- **FileVault and auto-login are mutually exclusive.** Auto-login (required for the LaunchAgent in the user session and for the AirPlay bridge) is disabled when FileVault is on. Either turn FileVault off on the hub Mac or accept a manual login after each reboot.
- **"Check for updates" (SET-2) has no defined source.** Decide: `git pull` on the hub checkout plus dependency sync, or a release tarball. Either way the hub needs a version string to display.
- **Restart from the UI** is simply the process exiting non-zero; `KeepAlive` brings it back. Worth one line so nobody builds a supervisor.

---

## 6. Smaller notes

- **Success metric 4** ("household stops opening vendor apps") is unmeasurable as written. Proxy: hub commands per day, trending up over the first 30 days.
- **Phase 3 sign-in flows** become Tidal OAuth device-code plus HEOS account sign-in if the "browse once" recommendation is adopted. Update the phase table accordingly.
- **Pandora single-stream** applies to PAN-1 and PAN-2 used together, which is the merged-list UI's implied use case. The UI should say so rather than fail silently.
- **Design system**: consistent and buildable. Token contrast checks out (`text-primary` on `bg-base` about 14:1; every badge pairing clears 4.5:1). Only inconsistency found: §6.5 volume sheet interleaves devices "distinguished by glyph only," while §6.6 target picker also shows power state. Fine, just make sure the same room name appears in both. Fontshare's license permits self-hosting General Sans.

---

## 6a. Found during the Phase 1 build: HEOS cannot seek

The HEOS CLI protocol has no seek command (verified against the CLI specification and pyheos 1.x; player commands are play/pause/stop/next/previous/volume/mute/play-mode/queue only). Consequences:

- CTL-3 and CTL-4 apply to Sonos only. HEOS sides carry `supports_seek=false`, and the hub returns `unsupported_action` for seek and skip on HEOS targets. The PWA renders the scrubber and ±15 disabled for HEOS sides, as it already does for radio sources.
- CTL-6 drift correction cannot nudge the HEOS side. The sync engine should treat HEOS as the clock master and always correct by seeking Sonos, forward or backward. That covers both signs of drift and removes the leader/laggard decision.
- Position on HEOS is still readable via `player_now_playing_progress` events, so the scrubber can display position on HEOS; it just cannot be dragged.

## 7. Recommended changes to the build plan

1. Add two go/no-go gates to Phase 0: the concurrent-stream manual test (§1.1) and the Tidal-ID-to-both-refs spike (§2.1). Together they take under a day and de-risk Phases 3 and 4.
2. Add native group handling to Phase 1 and make "side" a concept in the state model.
3. Move HTTPS serving (Tailscale or mkcert) into Phase 2 exit criteria.
4. Adopt "browse via service API, play by ID" as the Phase 3 architecture; demote SoCo MusicService browsing to a fallback.
5. Add queue view and search as Phase 3 P1 items so the API surface is shaped for them.
6. Re-scope the Phase 6 HEOS spike as "yt-dlp plus hub proxy," time-boxed to two days.
7. Record the HEOS no-seek constraint (§6a) in the PRD CTL notes and §7, and make Sonos the corrected side in the sync engine.
