# Spike: Pandora stations on HEOS and Sonos (PRD PAN-1..3)

**Status: unverified on hardware.** Everything below is what the hub implements (Phase 5) and
what the code assumes about the two vendors' Pandora trees and refs. Run the checklist on the hub
Mac and correct this file; the adapters are written to be adjusted, not trusted.

Pandora is per-ecosystem: no hub-side account, no cross-system sync (each device session gets its
own track sequence, and no API selects a track), and one stream per account is typical. The hub
merges the two station lists by normalised name and plays natively on each side.

## HEOS (pyheos 1.x over the HEOS CLI)

- **Linked?** `browse/get_music_sources` lists source **1 = Pandora**; `available` true when the
  HEOS account has Pandora logged in. The hub reads this on connect (`_load_sources`).
- **Stations:** `browse(source_id=1)`. Assumed shape: the root returns either the stations
  directly (type `station`, `playable`) or one level of containers (e.g. "My Stations",
  "Thumbprint Radio", "Search") — the hub collects stations at the root and inside the first
  four browsable containers, one level deep only. Each station carries `container_id` (may be
  empty), `media_id`, `name`, `image_url`.
- **Play:** `browse/play_stream?pid=&sid=1&cid=&mid=` (CLI 4.4.7; pyheos `Heos.play_station`).
  `cid` is omitted when the item had none.
- **Now playing:** `player_now_playing_media` with `type: station`, `source_id: 1`. Position
  events still arrive (`player_now_playing_progress`); seek is impossible on HEOS regardless.
  `supported_controls` should show `play_next` and not `play_previous`.

## Sonos (SoCo 0.31 SMAPI)

- **Linked?** `/status/accounts` lists an account whose `service_type` is `236*256 + 7` =
  **60423** (Pandora sid 236). Its serial is the `sn` for URIs. Manual fallback:
  `HUB_SONOS_PANDORA_SN`.
- **Stations:** `MusicService("Pandora", device=<any zone>)`, then `get_metadata("root")` and
  containers below it (`canEnumerate`), up to two levels. Stations are `mediaMetadata` items with
  `itemType` `program` (older trees: `stream`) and `canPlay`. Art is `albumArtURI` or
  `streamMetadata.logo`. **Risk:** Sonos moved Pandora to app-link authentication; SoCo's SOAP
  client may need the household's token to browse. If `get_metadata` fails with an auth fault,
  the fallback is to read the stations the Sonos app already favourited (`/FV:2` favorites) and
  play those; note the outcome here.
- **Play:** `SetAVTransportURI` (SoCo `play_uri(start=True)`) with
  `x-sonosapi-radio:{urlencoded id}?sid=236&flags=8300&sn={sn}` and DIDL-Lite
  `object.item.audioItem.audioBroadcast`, item id `F00092020{urlencoded id}`, parent `R:0/0`,
  `desc` `SA_RINCON60423_X_#Svc60423-0-Token`. The player resolves the stream itself via
  `getMediaURI`.
- **Now playing:** `current_track_uri` starts with `x-sonosapi-radio:`; `current_track_meta_data`
  carries the track title/artist Pandora picked. The hub stamps the station ref on
  `now_playing.content_ref` while the URI stays Pandora.

## Merge rule (hub)

Key = station name, case-folded, non-alphanumerics collapsed to `-` ("Chill Radio" → `chill-radio`).
Same key on both vendors → one item with `availability {heos: true, sonos: true}`. Names that
differ only in punctuation merge; genuinely different stations with the same name on one vendor
get `-2`, `-3` suffixes.

## Telling outcomes apart (what the hub reports)

| Sonos symptom | `GET /api/browse/pandora/stations` → `linked.sonos` | `accounts[pandora].last_error` | Meaning |
|---|---|---|---|
| Account serial found, SMAPI browse returns items | `true` | null | Working |
| Account serial found, browse returns no items | `true`, no Sonos-only stations | null | Linked but the account has **no stations** (check the Sonos app) |
| No account serial (type 60423) and `HUB_SONOS_PANDORA_SN` unset | `false` | null | **Not linked** in the Sonos app |
| `MusicServiceAuthException` (`Client.AuthTokenExpired`, `TokenRefreshRequired`) | `false` | "Sonos: Pandora on Sonos needs to be signed in again…" | **Auth fault**: SoCo's SMAPI client has no usable token; re-sign-in in the Sonos app, then `POST /api/browse/refresh`. If it persists, SoCo needs the household's app-link token (see fallback below) |
| Other `MusicServiceException` / SoCo error | `null` | "Sonos: RuntimeError: …" | **Hub bug or transient**: nothing cached, the next call retries; read the log line `station browse failed` |

HEOS: `linked.heos` is `false` when source 1 is unavailable in `get_music_sources`; a container
that fails to browse is skipped with the log line `station container browse failed` (the rest of
the list still comes back); `null` means the HEOS adapter is disconnected.

## Hardware checklist (hub Mac, both apps linked to Pandora)

- [ ] `GET /api/health` shows `heos` and `sonos` connected.
- [ ] `GET /api/browse/pandora/stations`: every station you see in the HEOS app and the Sonos app
      appears once; `availability` matches which app has it. Record the raw HEOS `browse` tree
      shape (hub log line `station browse`) and the Sonos root item types.
- [ ] `POST /api/play {"target": "<heos side>", "content_ref": {...station}}`: the receiver plays
      the station within ~3 s; `now_playing.source == "pandora"`, `content_ref` is the station,
      `supports_prev` false. `POST /api/transport/next` skips a track.
- [ ] Same on the Sonos side. If it fails, capture the UPnP error code (SoCo raises
      `SoCoUPnPException`; 714 = illegal MIME/URI, 800-series = service auth) and try `flags=32`
      and without `flags`.
- [ ] Before either: read `linked` on the stations page and `last_error` on the Pandora settings
      row and classify with the table above (no stations vs auth fault vs hub bug). Only the last
      column's "hub bug" row is an illyHub issue; the others are Sonos-app state.
- [ ] Choose a different station from the Sonos app while the hub shows one: `now_playing.content_ref`
      must clear (the hub matches the station id in the playing URI). Same on HEOS (station name).
- [ ] Start a station on the second side while the first plays: note whether Pandora pauses the
      first (the ack carries `pandora_concurrent` either way).
- [ ] Write the results on ai-dev #61/#62 and fix this file.
