# Hub API

The hub exposes REST for commands and a WebSocket for state. Everything is JSON. Every HTTP
response carries an `x-correlation-id` header (echoed from the request or generated); the same id
appears in the command's ack so a client can match an optimistic UI update to its outcome.
The live OpenAPI document is at `/docs` and `/openapi.json` on the running hub.

**Request-origin policy (CSRF and DNS rebinding).** The hub has no user auth (trusted LAN), so
it protects state-changing requests structurally:

- **Host allow-list.** Requests whose `Host` is not one of the hub's own names get **400**.
  Defaults: `localhost`, `127.0.0.1`, the hub's LAN IP, `*.local`, plus `HUB_ALLOWED_HOSTS` and the
  hostnames of `HUB_ALLOWED_ORIGINS` (add the tailnet MagicDNS name and any mkcert name).
- **Origin check.** Any non-GET/HEAD request that carries an `Origin` header must be same-origin
  (Origin host:port equals the request `Host`), or have a hostname on the host allow-list, or be
  listed verbatim in `HUB_ALLOWED_ORIGINS`; otherwise **403** `{code: "forbidden_origin"}`.
  Requests without `Origin` (curl, scripts) pass. Reads are never blocked.
- **Restart.** `POST /api/hub/restart` additionally requires the custom header `X-Illyhub: 1`
  (403 `{code: "missing_header"}` without it), so browsers always preflight it. Clients should
  send `X-Illyhub: 1` on every POST; the hub only enforces it on restart today.

Status: Phase 3 (Tidal browse + home). Sync endpoints (Phase 4), Pandora (Phase 5) and
YouTube Music (Phase 6) land later.

## Concepts

- **Player** — one HEOS or Sonos device. Ids: `heos-<pid>`, `sonos-<RINCON uid>`.
- **Side** — one ecosystem's native group: `heos:<group or player id>`, `sonos:<group or player id>`.
  Transport and seek act on a side's coordinator. Solo players are their own side.
- **Zone** — a Denon amplifier zone, `denon-<host>:main` / `denon-<host>:zone2`.
- **Target** — a player id, a side id, or `"all"`. Transport/seek resolve a player id to its side;
  volume/mute resolve a side id to its members.
- **Capabilities** — on players and sides: `supports_seek`, `supports_power`, `can_group`,
  `supports_next`, `supports_prev`. HEOS players report `supports_seek: false` because the HEOS
  CLI protocol has no seek command. A side's `supports_seek` / `supports_next` / `supports_prev`
  also follow the current source (a radio station cannot seek or go back). Sides carry a derived
  `volume` (coordinator volume for solo sides, member average for groups, the Sonos convention)
  and `muted` (true when every member is muted).

## Read endpoints

| Method | Path | Returns |
|---|---|---|
| GET | `/api/health` | `{status, degraded, fake_devices, version, uptime_s, connections, last_error, discovery_last_run, state_version, static: {fonts, app}, art_cache: {entries, bytes, hits, misses} \| null, history: {ok, last_error}, vault: {ok, last_error}}` — always 200. `history.ok` false means plays are not being logged; `vault.ok` false means the vault file could not be opened (bad `HUB_VAULT_KEY` or undecryptable file) and accounts cannot persist across restarts |
| GET | `/api/devices` | `{players[], zones[], sides[], discovered[]}` |
| GET | `/api/art/{cache_key}?size=` | artwork bytes (see **Art proxy**) |
| GET | `/api/history?limit=` | `{items: HistoryItem[]}` (see **History and home**) |
| GET | `/api/home` | home-screen aggregate (see **History and home**) |
| GET | `/api/settings` | accounts, hub info, hardware (see **Settings**) |

## Art proxy

The hub is the only origin the client loads artwork from (CORS, mixed content, resolution
normalisation, design-system accent). Every `now_playing[*].art` (and, from Phase 3, every browse
item) is an **ArtRef**:

```json
{"url": "/api/art/3f9c0e1b2a7d4c5e6f708192", "cache_key": "3f9c0e1b2a7d4c5e6f708192",
 "accent": "#B8452F", "accent_is_safe": true}
```

| Field | Meaning |
|---|---|
| `url` | hub-relative; append `?size=96` (mini-player thumb), `320` (cards), `1080` (Now Playing hero) or `backdrop` (540px, pre-blurred 60px-equivalent, for the Now Playing background). No `size` means 320. Variants are never upscaled past the source's shorter side |
| `cache_key` | 24 lowercase hex chars, stable per artwork: `sha1(service:content_id)` when the canonical id is known, else `sha1(source_url)` |
| `accent` | dominant colour of the art, clamped to lightness 35–60 % and saturation ≤ 70 % (design §2.2), or `null` |
| `accent_is_safe` | `true` only when `accent` is non-null. The two never disagree |

**Accent rules (exactly these):** `accent` is `null` and `accent_is_safe` is `false` when
(a) extraction fails or the image cannot be decoded, (b) the artwork is effectively greyscale —
the dominant colour's saturation is under 10 % before clamping, because a grey tint is pointless,
or (c) the clamped colour still fails the 4.5:1 check for `text-primary` over the accent tinted at
40 % on `bg-base`. Otherwise `accent` is the clamped hex and `accent_is_safe` is `true`. The
client tints the Now Playing backdrop and scrubber only when `accent_is_safe`; otherwise it uses
`bg-raised`. Both fields start `null`/`false` and arrive later as a normal `now_playing` delta once
the proxy has fetched and analysed the image. The client never computes colour from pixels
(design §11).

`GET /api/art/{cache_key}?size=…` responses:

| Status | When | Headers |
|---|---|---|
| 200 `image/jpeg` | cached variant | `ETag`, `Cache-Control: public, max-age=2592000, immutable` |
| 304 | `If-None-Match` matches (weak `W/` and comma lists accepted) | `ETag`, `Cache-Control` |
| 200 `image/png` | **fallback**: upstream fetch/decode failed, body too large (> 16 MB), refused scheme, or the proxy is disabled. Body is a neutral `bg-raised` square so an `<img>` never breaks | `X-Art-Fallback: upstream_error \| proxy_disabled \| placeholder_key`, `Cache-Control: no-store`, no `ETag`. Upstream is retried at most once a minute |
| 404 `{code:"unknown_art", message}` | key never registered, or evicted | |
| 400 `{code:"invalid_key"\|"invalid_size", message}` | key not 24 hex chars; `size` not one of `96, 320, 1080, backdrop` | |

Variants are square-fit JPEG (q85); transparent sources are composited onto `bg-raised`. Cache
lives on disk under `HUB_DATA_DIR/art/<key>/` with a 30-day TTL (`HUB_ART_TTL_DAYS`) and a size
budget (`HUB_ART_MAX_MB`, default 500, LRU by fetch time), swept daily. Service-direct sources
(Tidal CDN 1280px, YouTube Music thumbnails ≥ 640px) beat the device-reported URL when the adapter
knows the service ids (Phase 3); until then the device URL is the source and is normalised to the
sizes above. Only `http`/`https` sources are fetched (`fake://` synthetic art exists solely under
`HUB_FAKE_DEVICES=1`). `/api/health` reports `art_cache: {entries, bytes, hits, misses}`.

**`HUB_ART_PROXY=0`:** the hub still hands out hub-relative refs (`cache_key` is
`000000000000000000000000`) and every one serves the placeholder with
`X-Art-Fallback: proxy_disabled`. Device URLs are never sent to the client in either mode.

## Static serving

The hub serves the PWA export (`HUB_APP_DIR`, default `../app/out`) at `/` with SPA fallback
(`/now-playing` → `now-playing.html` or `index.html`), and self-hosted fonts from
`HUB_FONTS_DIR` at `/fonts/…`. `/api/*`, `/ws`, `/docs`, `/openapi.json` never fall back.
Reserved exactly: `api`, `ws`, `docs`, `redoc`, `openapi.json`, plus anything under `api/`.
Cache headers: `_next/static/*` and `/fonts/*` immutable for a year; HTML, `sw.js`,
`service-worker.js` and `manifest.webmanifest` `no-cache`; everything else one hour.
Unknown paths on **any** method return the 404 envelope `{code: "not_found", message}` (never
FastAPI's `{detail}` or a 405). When the export is missing the hub logs a warning, serves only
the API, and `/api/health` shows `static: {fonts, app}` as false.

TLS: `HUB_TLS_CERT` + `HUB_TLS_KEY` (both or neither; relative paths resolve against `hub/`;
both files must exist or the hub refuses to start) make uvicorn serve HTTPS directly (the mkcert
path). The default path is `tailscale serve`, which terminates TLS in front of plain
HTTP on 8080; see `ops/RUNBOOK.md` § HTTPS.

## Command endpoints

All return an **Ack** (below). HTTP status is 200 when `ok` is true, otherwise the status mapped
from the error code.

| Method | Path | Body | Notes |
|---|---|---|---|
| POST | `/api/transport/{action}` | `{target}` | `action` ∈ `play, pause, toggle, stop, next, prev` |
| POST | `/api/seek` | `{target, position_ms}` | clamped to the track duration; refused for non-seekable sides |
| POST | `/api/skip` | `{target, delta_ms = 15000}` | relative seek. Rapid skips accumulate: each adds to the still-pending target if one exists, else to the hub's position estimate extrapolated to now while playing. Clamped to `[0, duration]` |
| POST | `/api/volume` | `{target, level}` | per player; a side target sets every member |
| POST | `/api/volume` | `{linked: true, level}` or `{linked: true, delta}` | master move: the loudest online player is the master, ratios preserved; all-zero players rise together |
| POST | `/api/mute` | `{target, muted}` | |
| POST | `/api/zone/power` | `{zone_id, on}` | Denon zone via the amplifier link. A Sonos player/side id with `on: false` means stop + leave group; `on: true` is a no-op |
| POST | `/api/group` | `{vendor, coordinator_id, member_ids[]}` | **desired** membership, coordinator first. The adapter reconciles against the current topology: members not listed leave, new ones join. Cross-vendor lists are `invalid_argument` |
| DELETE | `/api/group/{side_id}` | | dissolve the side. HEOS: one `group/set_group` with the leader's id (the only way HEOS removes a group). Sonos: every non-coordinator `unjoin`s |

**Multi-target commands** (`target: "all"`, side targets for volume/mute, linked volume) apply
to every target that can be applied. `ok` is false only when nothing succeeded; the targets that
failed are listed in `partial` either way (see Ack).

Volume and seek writes are **coalesced** per target: at most one device write per
`HUB_COMMAND_COALESCE_S` (default 0.1 s), latest value wins. The first write in a burst runs
immediately and its failure fails the ack. A later write in the same window is *accepted*, not
yet written: its ack says `ok: true`, the device write happens when the window closes, and a
failure there is logged (the next state delta will show the true value).

### Ack

```json
{
  "correlation_id": "3f9c1a2b7e40",
  "ok": true,
  "action": "play",
  "target": "all",
  "state_version": 412,
  "error": null,
  "partial": [
    {"target": "heos:heos-1", "code": "device_offline", "message": "Living Room Amp is offline."}
  ],
  "applied": ["sonos:sonos-gRINCON_…"]
}
```

`applied` lists the targets (side or player ids) the command actually reached.

Exactly one ack is emitted per correlation id, whatever happens inside the hub (an unexpected
exception becomes `vendor_error`). `state_version` is the state version the router observed
when it finished: a **lower bound**, not proof of effect, because device writes may still be in
flight or coalesced and the resulting state change arrives later as a delta. `partial` lists the
targets that failed when at least one target was attempted and the failures are not the whole
story (a single failed target uses `error` alone).

### Error envelope

`error` is null on success, otherwise:

```json
{"code": "device_offline", "message": "Patio is offline.", "target": "sonos-RINCON_…", "correlation_id": "3f9c…"}
```

Messages are plain statements of what did not happen, suitable for a toast as-is.

| code | HTTP | Meaning |
|---|---|---|
| `unknown_target` | 404 | No player, side, zone, or `all` matches the target. |
| `unsupported_action` | 409 | The protocol for this device cannot perform the action (e.g. seek on HEOS). |
| `not_seekable` | 409 | The current source (radio station) does not support seek. |
| `adapter_disconnected` | 503 | The hub has no live connection to that ecosystem right now. |
| `device_offline` | 409 | The target device is known but not reachable. |
| `invalid_argument` | 400 | A parameter the schema cannot check: volume level outside 0–100 via the router, negative seek, linked volume with neither `level` nor `delta`, or a group list that crosses vendors. (Shape/type violations return FastAPI's 422 before the router runs.) |
| `vendor_error` | 502 | The device rejected the command or returned an error. |

## Accounts (auth)

Streaming accounts are linked to the **hub**, not to the client. Tokens live in an encrypted
vault under `HUB_DATA_DIR` (`vault.json`, Fernet key from `HUB_VAULT_KEY` or the generated
`vault.key`, mode 0600 — that file must never leave the hub Mac). Nothing about tokens is ever
returned by the API or written to logs.

Tidal uses the device-code ("link") flow: the hub shows a code, the user approves it at
`link.tidal.com` on any device, the hub notices in the background.

| Method | Path | Returns |
|---|---|---|
| POST | `/api/auth/tidal/start` | `{service: "tidal", user_code, verification_url, expires_in_s, interval_s}`. The hub polls Tidal until approval or expiry. Calling again starts a fresh code |
| GET | `/api/auth/tidal/status` | **AccountStatus** `{service, linked, state, account_name, expires_at, pending: {user_code, verification_url, expires_at} \| null, last_error}` |
| POST | `/api/auth/tidal/unlink` | AccountStatus with `linked: false`; tokens deleted, browse cache cleared |

`state` is what the client renders: `linked` (account usable), `pending` (a device code is
waiting for approval; show the code), `restoring` (the hub found stored tokens at startup and is
validating them; render a skeleton, not "Connect Tidal"), `unlinked`. `linked` stays a boolean
for convenience and equals `state == "linked"`.

Access tokens refresh transparently before browse calls. A refresh failure unlinks the account
and the next browse returns `needs_link`. Unlinking during a pending flow cancels it; a late
approval of a cancelled flow is ignored. Starting a new flow cancels the previous one.

**`needs_link` envelope** — returned as HTTP 409 by every browse/play endpoint when the hub has
no account for the service:

```json
{"code": "needs_link", "message": "Tidal is not connected. Link it in Settings.", "service": "tidal"}
```

## Browse (Tidal)

"Browse once, play anywhere": the hub reads the library through Tidal's own API and every item
carries the canonical Tidal id. Playback on each ecosystem is built from that id (see **Play**).

| Method | Path | Returns |
|---|---|---|
| GET | `/api/browse/tidal/favorites/albums?limit=50&offset=0` | **BrowsePage** of albums |
| GET | `/api/browse/tidal/favorites/playlists?limit=&offset=` | BrowsePage of saved (other people's) playlists |
| GET | `/api/browse/tidal/playlists?limit=&offset=` | BrowsePage of the user's own playlists |
| GET | `/api/browse/tidal/album/{id}` | **Container** `{item, tracks[]}` |
| GET | `/api/browse/tidal/playlist/{id}` | Container |
| POST | `/api/browse/refresh` | `{cleared}` — drops the browse cache (5 min TTL, `HUB_BROWSE_CACHE_S`) |

```json
// BrowsePage
{"items": [BrowseItem], "offset": 0, "limit": 50, "total": 6, "next_offset": null}

// BrowseItem
{"content_ref": {"service": "tidal", "kind": "album", "id": "101"},
 "title": "Warm Glow", "subtitle": "Analog Heart",
 "art": {"url": "/api/art/…", "cache_key": "…", "accent": null, "accent_is_safe": false},
 "duration_ms": 1926000, "track_count": 9,
 "availability": {"heos": true, "sonos": true},
 "index": null, "album_id": null, "artist": "Analog Heart", "album": null}
```

- `content_ref` is the canonical id: `service` ∈ `tidal | ytmusic | pandora`, `kind` ∈
  `album | playlist | track | station`. Pass it back verbatim to `POST /api/play`.
- `availability` says which **ecosystems** can play the item: `heos` is true when the HEOS
  account has Tidal linked (`browse/get_music_sources`), `sonos` when the household has a Tidal
  account (Sonos account serial found, or `HUB_SONOS_TIDAL_SN`). The client greys sides that
  cannot play the content in the target picker; the hub also refuses with
  `not_available_on_side`.
- Tracks (inside a Container) carry `index` (0-based position, for "play from here"),
  `album_id`, `artist`, `album`, `duration_ms`.
- Art goes through the art proxy like now-playing art; Tidal covers are fetched from the Tidal CDN
  at 1280 px (service-direct, PRD §3.4).
- Unknown album/playlist/track → 404 `{code: "not_found", message, content_ref}`.
- Album and playlist track lists are complete (the hub walks Tidal's 100-item pages). Favorites
  pages are capped at 50 by Tidal; `next_offset` is null once a page comes back short.
- `content_ref.id` must match `^[A-Za-z0-9_.:-]+$` (422 otherwise).

## Play

`POST /api/play` `{target, content_ref, start_index = 0}` → **Ack** (`action: "play_content"`).

The hub resolves the album/playlist to its track list, then on every target side replaces the
queue and starts at `start_index`. Sonos: Tidal URIs + DIDL metadata built from the ids
(`x-sonos-http:track/{id}.flac?sid=174&flags=8224&sn={sn}`); HEOS: `browse/add_to_queue`
(replace and play) with the Tidal source id 10. The exact ref formats are in
`docs/spikes/tidal-refs.md` and are **unverified on hardware** until ai-dev #10 runs.

Ack additions: `applied` lists the side ids the content actually started on. Error codes added
for this endpoint:

| code | HTTP | Meaning |
|---|---|---|
| `needs_link` | 409 | The hub has no account for the service (link it in Settings). Raised before any side is touched, as the `needs_link` envelope above rather than an Ack |
| `not_available_on_side` | 409 | That ecosystem has no account for the service (link it in the vendor app). Other sides still play; listed in `partial` |

Every successful play writes a history row (below).

## History and home

The hub keeps its own play log in SQLite (`HUB_DATA_DIR/history.sqlite`, retention
`HUB_HISTORY_MAX_ROWS`, default 500): neither HEOS nor Sonos exposes listening history, so
"Recently played" only knows plays routed through the hub.

`GET /api/history?limit=20` → `{items: HistoryItem[]}`, newest first, one row per content ref:

```json
{"content_ref": {"service": "tidal", "kind": "album", "id": "101"},
 "title": "Warm Glow", "subtitle": "Analog Heart", "art": {…},
 "last_played_at": "2026-09-07T22:10:04.120Z",
 "last_targets": ["heos:heos-1"], "play_count": 3, "sync": false}
```

`last_targets` are the side ids of the most recent play, which the target picker pre-highlights
(PRD Decision 4).

`GET /api/home` → one call for the home screen:

```json
{"recents": [HistoryItem],
 "playlists":       {"items": [BrowseItem], "needs_link": null},
 "favorite_albums": {"items": [BrowseItem], "needs_link": null},
 "stations":        {"items": [], "needs_link": null}}
```

- `playlists` merges the user's own and saved Tidal playlists (YouTube Music joins in Phase 6),
  sorted by recency of play (hub history) then alphabetically. Same-named playlists across
  services are **not** deduped; the badge disambiguates.
- A section whose service is not linked returns `items: []` with `needs_link: "tidal"` instead of
  failing the whole call; the client renders the "Connect Tidal" card there. A section whose
  loader fails for any other reason returns `items: []` with `error: "<summary>"`; recents and the
  other sections still render.
- `stations` is a Phase 5 placeholder and is always empty for now.
- Warm responses come from the browse cache; the hub logs `home_ms` per call.

## Settings

`GET /api/settings`:

```json
{"accounts": [
   {"service": "tidal", "linked": true, "account_name": "james", "expires_at": "…", "pending": null, "last_error": null},
   {"service": "ytmusic", "linked": false},
   {"service": "pandora", "linked": true},
   {"service": "heos_account", "linked": true, "account_name": "james@…"}],
 "hub": {"address": "192.168.1.10", "port": 8080, "https": false, "version": "0.1.0", "uptime_s": 4021.3, "fake_devices": false},
 "hardware": [{"id": "heos-1", "name": "Living Room Amp", "vendor": "heos", "kind": "player", "model": "Denon AVR-X3700H", "ip": "…", "online": true},
              {"id": "denon-10.0.0.5:main", "name": "Main zone", "vendor": "denon", "kind": "zone", "ip": "10.0.0.5", "online": true}]}
```

`pandora.linked` reflects whether either ecosystem reports Pandora as linked (Pandora is played
natively per ecosystem, PRD §3.5); `ytmusic` is a placeholder until Phase 6.

`POST /api/hub/restart` (requires `X-Illyhub: 1`) → 202 `{restarting: true}`; the process exits
with code 0 half a second later and launchd's `KeepAlive=true` relaunches it regardless of exit
code. Repeated calls while a restart is scheduled return 202 without scheduling another exit.
409 `{code: "restart_disabled"}` when `HUB_ALLOW_RESTART=0`.

## Dev endpoint (fake devices only)

Mounted only when `HUB_FAKE_DEVICES=1`. Simulates changes made outside the hub so the PWA and
its Playwright suite can exercise state handling. With `HUB_FAKE_TIDAL=1` the browse, play, history
and home endpoints serve a canned Tidal library (6 albums, 4 playlists) and the fakes keep a real
queue per side: `next`/`prev` walk it and now-playing follows.

`POST /api/dev/fake/{scenario}` → `{scenario, result, state_version}`

| scenario | Effect |
|---|---|
| `track_change` | HEOS side advances to the next fake track |
| `volume_change` | Patio volume +10 (wraps) |
| `disconnect_heos` | HEOS adapter drops to `reconnecting`, its players go offline |
| `reconnect_heos` | HEOS adapter back to `connected` |
| `sonos_regroup` | Toggles Kitchen + Patio between grouped and solo |
| `link_tidal` / `unlink_tidal` | With `HUB_FAKE_TIDAL=1`: link/unlink the canned Tidal library everywhere at once (hub account and both vendor apps), so `needs_link` and `not_available_on_side` paths can be exercised |
| `approve_tidal` | Completes a pending fake link flow immediately. (`POST /api/auth/tidal/start` in fake mode goes `pending` for about a second, then `linked`, so the pending UI can be exercised.) |

Unknown scenario → 404 with the list of valid names.

## WebSocket `/ws`

All frames are JSON objects with a `type`.

**Server → client**

| type | fields | when |
|---|---|---|
| `snapshot` | `version`, `state` (full HubState) | on connect, after `resync`, and after the client's outbound queue overflowed (queue is cleared first) |
| `delta` | `from_version`, `to_version`, `changed` | every state commit. `changed` maps a depth-two path (`players.heos-1`, `sides.sonos:…`, `positions.…`, `connections.heos`, `sync`) to its **full new value**, or `null` when removed. Apply by replacing that key. |
| `ack` | the Ack fields | every command issued over REST, **broadcast to every client**. The `correlation_id` is the routing key: a client matches acks to its own requests by the id it sent (or received in the `x-correlation-id` response header) and ignores the rest. Acks travel on a small separate queue that a state-queue overflow does not clear |
| `ping` | | every 20 s; reply with `pong`. Two missed pongs close the socket |
| `pong` | `server_time_ms` | reply to a client `ping` |
| `error` | `code: "invalid_message"`, `message` | malformed client frame (not JSON, or unknown `type`); connection stays open |

**Client → server**: `{"type": "ping"}`, `{"type": "pong"}`, `{"type": "resync"}`.

If a client receives a `delta` whose `from_version` is not the version it holds, it should send
`resync` and replace its state with the next `snapshot`.

**Ack timeout (client side).** The REST response already carries the ack, so the WebSocket
copy is for other clients and for reconciling optimistic UI. A client that issued a command
and has not seen its ack on the socket within **5 s** should treat the command as unconfirmed,
revert the optimistic state, and send `resync`.

### HubState shape (abridged)

```json
{
  "version": 412,
  "players":     {"heos-1": {"id", "name", "vendor", "ip", "model", "online", "volume", "muted", "play_state", "group_id", "capabilities"}},
  "zones":       {"denon-10.0.0.5:main": {"id", "key", "name", "power", "online", "host", "device_id", "player_ids"}},
  "groups":      {"sonos-gRINCON_…": {"id", "vendor", "coordinator_player_id", "member_ids", "name"}},
  "sides":       {"sonos:sonos-gRINCON_…": {"id", "vendor", "coordinator_player_id", "member_ids", "name", "play_state", "volume", "muted", "capabilities"}},
  "now_playing": {"<side id>": {"title", "artist", "album", "art": {"url", "cache_key", "accent", "accent_is_safe"}, "source", "seekable", "supports_next", "supports_prev", "duration_ms", "track_id"}},
  "positions":   {"<side id>": {"position_ms", "reported_at", "confidence"}},
  "sync":        {"status", "side_ids", "drift_ms", "last_correction_at"},
  "connections": {"heos": {"state", "last_error", "since"}, "sonos": {...}, "denon": {...}}
}
```

`capabilities` on every player/side: `{supports_seek, supports_power, can_group, supports_next, supports_prev}`.

`positions` are hub estimates. Both ecosystems report whole seconds; the hub timestamps tick
edges and interpolates, so `position_ms` has sub-second resolution and `confidence` is `0.9` once
the estimate is tight (under ~250 ms), `0.5` before or right after a seek/track change (an
optimistic value written before the device confirms), `1.0` when paused (exact). Clients should
interpolate forward from `reported_at` while `play_state` is `play`.
