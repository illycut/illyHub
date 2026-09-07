# Hub API

The hub exposes REST for commands and a WebSocket for state. Everything is JSON. Every HTTP
response carries an `x-correlation-id` header (echoed from the request or generated); the same id
appears in the command's ack so a client can match an optimistic UI update to its outcome.
The live OpenAPI document is at `/docs` and `/openapi.json` on the running hub.

Status: Phase 1 (core control). Browse, play-by-content, history, art, sync, auth, and settings
endpoints from PRD §4.5 land in later phases.

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
| GET | `/api/health` | `{status, degraded, fake_devices, version, uptime_s, connections, last_error, discovery_last_run, state_version}` — always 200 |
| GET | `/api/devices` | `{players[], zones[], sides[], discovered[]}` |

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
  ]
}
```

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

## Dev endpoint (fake devices only)

Mounted only when `HUB_FAKE_DEVICES=1`. Simulates changes made outside the hub so the PWA and
its Playwright suite can exercise state handling.

`POST /api/dev/fake/{scenario}` → `{scenario, result, state_version}`

| scenario | Effect |
|---|---|
| `track_change` | HEOS side advances to the next fake track |
| `volume_change` | Patio volume +10 (wraps) |
| `disconnect_heos` | HEOS adapter drops to `reconnecting`, its players go offline |
| `reconnect_heos` | HEOS adapter back to `connected` |
| `sonos_regroup` | Toggles Kitchen + Patio between grouped and solo |

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
  "now_playing": {"<side id>": {"title", "artist", "album", "art": {"url", "accent", "accent_is_safe"}, "source", "seekable", "supports_next", "supports_prev", "duration_ms", "track_id"}},
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
