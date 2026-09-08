# Spike: YouTube Music on HEOS via yt-dlp and a hub stream proxy (ai-dev #67, PRD LIB-5)

**Verdict: no-go for v1. HEOS sides stay `YouTube Music isn't available on HEOS.`**
Time box respected: this is a desk evaluation of the two tools plus the HEOS protocol, not an
implementation. The route `GET /api/stream/ytmusic/{video_id}` is reserved and returns
`501 not_implemented` pointing here.

## What was checked

- `yt-dlp` 2026.08 installs cleanly in an isolated env (`uv run --with yt-dlp`); `ffmpeg` is on
  the hub Mac's PATH via Homebrew. Neither was added to the hub's dependencies.
- `ytmusicapi.get_song(videoId)` returns `streamingData` only with a signature timestamp and the
  URLs it exposes are the same expiring googlevideo URLs `yt-dlp` resolves; there is no
  stable per-track audio URL anywhere in the API.
- HEOS `browse/play_stream` (CLI 4.4.4) takes a URL and plays it as a station-like stream: no
  queue, no gapless, no track metadata beyond what the hub sends, and HEOS reports
  `type: station` so seek/prev/next are meaningless.

## Findings

| Concern | Finding | Consequence |
|---|---|---|
| URL expiry | googlevideo URLs carry `expire=` about 6 hours out and are bound to the requesting IP. Resolved from the hub Mac they are valid for the hub, not for the receiver. | The hub must proxy every byte; the receiver can never be handed the URL directly. |
| Format | Best audio is adaptive (DASH `webm/opus` or `m4a`) with no progressive MP3/AAC. | `ffmpeg` remux (`-c copy` to ADTS/AAC for m4a; transcode for opus) per track, streamed. |
| Track boundaries | `play_stream` is one URL. An album is N URLs. Either the proxy concatenates tracks into one endless stream (loses track metadata and HEOS `next`) or the hub issues a new `play_stream` per track on end-of-stream detection (gap of 1–3 s, HEOS shows "buffering"). | Neither meets the design bar for "browse to play" content; it is radio-grade at best. |
| Position / drift | HEOS reports stream position from stream start, not track position; the hub's PositionTracker would need per-track offsets. Sync Play is out regardless (Tidal-only contract). | Scrubber stays read-only (HEOS cannot seek anyway), timecodes need proxy-side bookkeeping. |
| CPU | `ffmpeg -c copy` is trivial; opus→AAC transcode is ~0.3 core per stream on the 2019 i9. One HEOS side at a time keeps it under 5%. | Acceptable if it were built. |
| Fragility | yt-dlp needs updates every few weeks to keep extracting; a stale binary breaks playback silently. The hub would need a self-update path for yt-dlp too. | Operational burden on a household appliance. |
| Terms | Extracting and re-streaming YouTube media bypasses the YouTube client and violates the YouTube Terms of Service. Sonos plays YouTube Music through Google's licensed partner integration; HEOS has no such integration. | Not something to ship in a product the household relies on. |

## Recommendation

1. YouTube Music ships **Sonos-native only** (PRD Decision 1's fallback). The app's target picker
   shows HEOS rooms disabled with "YouTube Music isn't available on HEOS." and Sync Play is
   refused for YouTube Music content.
2. If the household wants YouTube Music in a HEOS room, the supported path is the vendor one:
   AirPlay from a phone or the Mac to the Denon (Phase 7's AirPlay bridge could later drive
   this the same way it drives Pandora sync). That keeps playback inside a licensed client.
3. Revisit only if Denon adds a YouTube Music source to HEOS (check `browse/get_music_sources`
   on the receiver after firmware updates; the hub logs the source list at connect).

## If someone still wants to try it (not recommended)

- `pip install yt-dlp` into a separate venv; `yt-dlp -f bestaudio -g https://music.youtube.com/watch?v=<id>` prints the expiring URL.
- Proxy sketch: FastAPI `StreamingResponse` wrapping `ffmpeg -i <url> -vn -c:a aac -f adts -` for
  m4a input, `-c copy -f adts` when already AAC; set `Content-Type: audio/aac`; send to HEOS with
  `browse/play_stream pid=<pid> url=http://<hub>:8080/api/stream/ytmusic/<id>`.
- Expect HEOS to show the stream as a station; expect a 1–3 s gap per track.
