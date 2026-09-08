# Spike: Tidal id → HEOS and Sonos playable refs (ai-dev #10)

**Status: implemented in the hub, unverified on hardware.** The formats below are what the
Phase 3 hub sends. Run the checklist at the bottom on the hub Mac and record results here.

## Why

"Browse once, play anywhere" (prd-review §2.1): the hub browses Tidal through `tidalapi` and
plays on each ecosystem by constructing that device's reference from the canonical Tidal id.
No SoCo MusicService browsing, one auth flow, and Sync Play keys on the same ids.

## Sonos (`hub/src/illyhub_hub/adapters/sonos.py`)

Per track, a `DidlMusicTrack` (SoCo builds the DIDL-Lite XML):

| Field | Value |
|---|---|
| resource URI | `x-sonos-http:track/{track_id}.flac?sid=174&flags=8224&sn={sn}` |
| protocolInfo | `sonos.com-http:*:audio/flac:*` |
| item id | `10032020track/{track_id}` |
| parent id | album: `1004206calbum/{album_id}` · playlist: `1006006cplaylist/{playlist_id}` · else `10032020` |
| upnp:class | `object.item.audioItem.musicTrack` (SoCo default for `DidlMusicTrack`) |
| desc | `SA_RINCON44551_X_#Svc44551-0-Token` (Tidal service type 44551 = 174·256 + 7) |

Queue load on the coordinator, in a worker thread: `clear_queue()` → `add_multiple_to_queue`
in batches of 16 → `play_from_queue(start_index)`.

`sn` is the Sonos **account serial** for the household's Tidal account, read from
`/status/accounts` via `soco.music_services.accounts.Account.get_accounts()` (the account whose
`service_type == "44551"`). If the lookup fails, set `HUB_SONOS_TIDAL_SN` from the runbook §0
`curl http://SONOS_IP:1400/status/accounts` output (`SerialNum` of the Tidal account).

Known unknowns to verify: `flags=8224` vs `flags=8232` (both appear in captures of the Sonos app,
8224 is the common one for tracks); whether `.flac` vs `.mp4` matters for HiFi vs Max tiers;
whether the player accepts the item without `albumArtURI`.

## HEOS (`hub/src/illyhub_hub/adapters/heos.py`)

`browse/add_to_queue` (HEOS CLI 4.4.11 / 4.4.12) via pyheos `HeosPlayer.add_to_queue`:

| Content | sid | cid | mid | aid |
|---|---|---|---|---|
| album | 10 (Tidal) | `{album_id}` | — | 4 (replace and play) |
| playlist | 10 | `{playlist_id}` | — | 4 |
| single track | 10 | `{album_id}` (or empty) | `{track_id}` | 4 |

Start mid-list: the container loads asynchronously, so the hub polls `player/get_queue` until at
least `start_index + 1` items are present (5 s timeout), then `player/play_queue` with
`qid = start_index + 1`. If the client cannot read or play the queue the play **fails** (the ack
never reports `applied` for a dropped `start_index`).

Known unknowns to verify: whether HEOS accepts the bare Tidal album id as `cid` or requires the
container id string it returns from its own `browse/browse` of the Tidal source (in captures
the two are the same digits, but the CLI documents `cid` as opaque). If they differ, the
fallback is one `browse/browse` of `sid=10` → "My Albums" to map ids, cached.

Availability: the HEOS side reports Tidal as linked when `browse/get_music_sources` lists
source id 10 as `available`.

## Verification checklist (hub Mac, real devices)

- [ ] `GET /api/settings` shows `tidal.linked: true` after the device-code flow.
- [ ] `GET /api/browse/tidal/favorites/albums` lists your albums with art; `availability.heos`
      and `.sonos` match which vendor apps have Tidal linked.
- [ ] `POST /api/play {target: "<sonos side>", content_ref: <album>}` starts track 1 on Sonos;
      the Sonos app shows the right title/artist and the track advances gaplessly.
- [ ] Same with `start_index: 3` on Sonos.
- [ ] `start_index: 3` on HEOS: confirm the queue poll settles quickly (log shows how many
      `get_queue` rounds it took) and playback starts on track 4, not track 1. If it times out,
      raise `QUEUE_WAIT_S` or record the observed load time here.
- [ ] `POST /api/play {target: "<heos side>", content_ref: <album>}` starts on HEOS.
- [ ] `POST /api/play {target: "all", …}` starts both; the ack `applied` lists both sides.
- [ ] A playlist on each side.
- [ ] Concurrent-stream test (ai-dev #9): does either side pause when the other starts?
- [ ] Record `sn`, `flags`, and any id mapping needed above; update this file and the adapters.
