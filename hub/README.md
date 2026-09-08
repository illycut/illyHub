# illyHub hub service

Python 3.12 / FastAPI service that owns the HEOS socket, Sonos UPnP subscriptions,
and the Denon control link, and exposes one normalized state model over REST and
WebSocket. See `../docs/multi-room-audio-PRD.md` and `../docs/api.md`.

## Setup

```
uv sync                # installs runtime + dev deps into .venv (Python 3.12 via uv)
cp .env.example .env   # edit as needed
```

System `python3` is not used; always run through `uv run`.

## Run

```
HUB_FAKE_DEVICES=1 uv run hub      # boots with protocol fakes, no hardware needed
uv run hub                         # real adapters (needs HUB_HEOS_HOST / HUB_DENON_HOST or SSDP)
```

Then `curl localhost:8080/api/health` and `curl localhost:8080/api/devices`.
OpenAPI docs at `http://localhost:8080/docs`.

## Endpoints (Phases 1–2)

```
GET    /api/health                      hub + connection status (always 200)
GET    /api/devices                     players, zones, sides, discovered devices
GET    /api/art/{cache_key}?size=       proxied artwork: 96 | 320 | 1080 | backdrop (blurred)
GET    /                                PWA static export (HUB_APP_DIR) with SPA fallback
GET    /fonts/...                       self-hosted General Sans (HUB_FONTS_DIR)
POST   /api/transport/{action}          {target}   play|pause|toggle|stop|next|prev
POST   /api/seek                        {target, position_ms}
POST   /api/skip                        {target, delta_ms=15000}
POST   /api/volume                      {target, level} | {linked: true, level|delta}
POST   /api/mute                        {target, muted}
POST   /api/zone/power                  {zone_id, on}   Denon zone, or Sonos id for stop+ungroup
POST   /api/group                       {vendor, coordinator_id, member_ids}
DELETE /api/group/{side_id}
WS     /ws                              snapshot, deltas, acks, ping/pong, resync
POST   /api/dev/fake/{scenario}         fakes only: track_change | volume_change |
                                        disconnect_heos | reconnect_heos | sonos_regroup
```

`target` is a player id, a side id, or `all`. Every command returns an ack
`{correlation_id, ok, action, target, state_version, error}`; the same ack is broadcast on
`/ws`. Error codes and the WebSocket protocol are documented in `../docs/api.md`.

Artwork: every `now_playing.art` is an `ArtRef` `{url, cache_key, accent, accent_is_safe}`
served by the hub (`docs/api.md` § Art proxy). Cache on disk under `HUB_DATA_DIR/art`
(default `hub/data`, gitignored), 30-day TTL, `HUB_ART_MAX_MB` LRU budget. `HUB_ART_PROXY=0`
keeps refs hub-relative but serves the neutral placeholder for every one.
Fakes use `fake://art/<name>` URLs, which the proxy renders as synthetic gradients so the PWA
has real-looking art and accents without hardware.

Known protocol limit: the HEOS CLI has no seek command, so HEOS sides report
`capabilities.supports_seek=false` and `/api/seek` / `/api/skip` on them return
`unsupported_action`.

## Test and coverage

```
uv run ruff check && uv run ruff format --check
uv run pytest --cov=illyhub_hub --cov-report=term-missing   # fails under 80% line coverage
```

## Layout

```
src/illyhub_hub/
  config.py        pydantic-settings (HUB_* env, .env)
  logsetup.py      JSON log formatter with correlation_id contextvar, optional rotating file
  state.py         HubState + sub-models, StateStore, diff()
  discovery.py     SSDP + static device registry, new-device callbacks (HEOS host handoff)
  adapters/        base interfaces + command ABCs, heos (pyheos), sonos (SoCo), denon, fake
  commands.py      CommandRouter: target resolution, error catalogue, acks, latency logging
  coalesce.py      per-target write coalescing for volume/seek bursts
  positions.py     sub-second position estimation from whole-second device reports
  art.py           art proxy: disk cache, 96/320/1080/backdrop variants, accent + contrast check,
                   service-direct resolver (Tidal CDN, YT Music thumbnails, device fallback)
  static.py        PWA export + fonts serving with SPA fallback and cache headers
  ws.py            WebSocket sessions: snapshot/delta/ack stream, heartbeat, bounded queues
  tasks.py         tracked fire-and-forget tasks, cancellation-safe stop_task()
  api.py           FastAPI app factory: read, command, ws, and dev routes
  main.py          uvicorn entrypoint (HUB_TLS_CERT/HUB_TLS_KEY → HTTPS)
```

## Verification status

Real adapters (`heos.py`, `sonos.py`, `denon.py`) are unit-tested against mocks only.
Nothing in this package has been run against physical hardware yet; see
`../ops/RUNBOOK.md` for the LAN verification checklist.

## Phase 3: accounts, Tidal browse, play by id, history, home, settings

- `auth/vault.py` — Fernet-encrypted JSON vault under `HUB_DATA_DIR` (`vault.json`); key from
  `HUB_VAULT_KEY` or generated once into `vault.key` (0600). Never copy that file off the Mac.
- `auth/tidal.py` — Tidal device-code link flow (`tidalapi`), transparent refresh, status.
- `services/tidal.py` — browse through Tidal's API (favorites, playlists, album/playlist tracks),
  5-minute cache, per-side availability; `FakeTidalCatalog` for `HUB_FAKE_TIDAL=1`.
- `content.py` — `ContentRef` / `BrowseItem` / `Container` shapes; `history.py` — SQLite play log.
- Adapters gained `play_content()` (Sonos: Tidal URIs + DIDL; HEOS: `browse/add_to_queue`) and
  `service_linked()`. Ref formats: `docs/spikes/tidal-refs.md` (unverified on hardware).
- Endpoints: `/api/auth/tidal/*`, `/api/browse/tidal/*`, `/api/browse/refresh`, `POST /api/play`,
  `/api/history`, `/api/home`, `/api/settings`, `POST /api/hub/restart`. Contract: `docs/api.md`.

Offline dev with a canned library: `HUB_FAKE_DEVICES=1 HUB_FAKE_TIDAL=1 uv run hub`, then
`POST /api/dev/fake/unlink_tidal` / `link_tidal` to exercise the not-linked paths.
