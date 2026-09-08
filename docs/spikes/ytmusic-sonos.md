# Spike: YouTube Music on Sonos from canonical ids (ai-dev #66)

**Status: implemented against assumptions, unverified on hardware.** Everything below was
derived from SoCo source, the Tidal path already in the hub, and community captures of Sonos
YouTube Music URIs. The first LAN run decides whether the assumed URI template holds; the
template is a setting so it can be corrected without a code change.

## Ids the hub has

From `ytmusicapi`: `videoId` (11 chars) for tracks, album `browseId` (`MPREb_…`), `playlistId`
(`PL…`, `OLAK5uy_…` for album auto-playlists, `LM` for Liked Music).

## Sonos service constants

| Item | Value | Source |
|---|---|---|
| Service id (`sid`) | `284` | `sonos.py` `SERVICE_IDS`; Sonos music services list |
| Service type | `72711` (= 284·256 + 7) | Sonos convention, same rule as Tidal 44551 / Pandora 60423 |
| Account serial (`sn`) | from `/status/accounts` entry with `service_type == "72711"` | `discover_ytmusic_sn`; fallback `HUB_SONOS_YTMUSIC_SN` |
| DIDL `desc` | `SA_RINCON72711_X_#Svc72711-0-Token` | same shape as Tidal/Pandora |
| protocolInfo | `sonos.com-http:*:audio/mp4:*` | YouTube Music streams are AAC in MP4 |

## Assumed refs (default template)

```
track URI   x-sonos-http:sonos-track:{videoId}.mp4?sid=284&flags=8232&sn={sn}
item id     00032020sonos-track:{videoId}
parent id   0004206calbum:{browseId}      (album context)
            0006206cplaylist:{playlistId} (playlist context)
            00032020                      (bare track)
```

Alternatives seen in captures, in case the default fails on hardware:

1. `x-sonosapi-hls-static:{videoId}?sid=284&flags=8&sn={sn}` (HLS variant, older firmware).
2. `x-sonos-http:{videoId}.mp4?sid=284&flags=8224&sn={sn}` (no `sonos-track:` prefix).

Set `HUB_SONOS_YTMUSIC_URI` to the working template, e.g.
`HUB_SONOS_YTMUSIC_URI="x-sonosapi-hls-static:{id}?sid=284&flags=8&sn={sn}"`. The item id prefix
is fixed at `00032020sonos-track:`, and the playlist parent prefix is assumed `0006206c` (Tidal
uses `1006006c` for playlists and `1004206c` for albums; YouTube Music may use either family).
If the player rejects the DIDL, capture a real queue entry
(`curl http://SONOS_IP:1400/status/…` or SoCo `get_queue()` while the Sonos app plays YouTube
Music) and file an illyHub issue with the `res` URI and `item id`.

## How to read the outcome on the hub Mac

1. `GET /api/settings` → accounts[ytmusic].linked true (hub side) and
   `GET /api/devices` → Sonos players present; `router.service_availability("ytmusic").sonos`
   is surfaced as `availability.sonos` on every YouTube Music browse item.
2. If `availability.sonos` is false while YouTube Music is linked in the Sonos app: the account
   serial lookup failed. Run on the hub Mac:
   `curl -s http://SONOS_IP:1400/status/accounts` and look for `Type="72711"`; put its `SerialNum`
   in `HUB_SONOS_YTMUSIC_SN` and restart.
3. `POST /api/play {target: <sonos side>, content_ref: {service: ytmusic, kind: album, id}}`:
   - ack `ok` and audio → template correct; done.
   - ack `ok` but silence or the Sonos app shows "unable to play": template wrong → try
     alternatives 1–2 via `HUB_SONOS_YTMUSIC_URI`, restart, retry.
   - `vendor_error` with UPnP 714/716 in the log → DIDL rejected → capture a real entry (above).
4. `now_playing.content_ref.service == "ytmusic"` and `source == "ytmusic"` on the Sonos side
   confirm the URI parser (`ytmusic_ref_from_uri`) matches the live URI.

## Checklist

- [ ] Capture one real queue entry while the Sonos app plays a YouTube Music **playlist** and note
      the parent-id prefix (`0006206c…` vs `1006006c…`) and item-id prefix; file the exact strings.

- [ ] `/status/accounts` shows a Type 72711 entry; `availability.sonos` is true.
- [ ] Album, playlist and single track each start on a Sonos side from `/api/play`.
- [ ] Starting at `start_index` 3 lands on the fourth track.
- [ ] Now Playing on the Sonos side carries `content_ref.service ytmusic` and the YouTube Music badge.
- [ ] Recents rail shows the item with `availability {heos: false, sonos: true}`.
- [ ] HEOS target refuses with "YouTube Music isn't available on HEOS." (expected, ai-dev #67).
