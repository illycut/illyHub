# Spike: the hub Mac as an AirPlay 2 sender ("Pandora Sync", PRD §3.5 PAN-4)

**Status:** spike done September 7, 2026 on the dev Mac (Apple Silicon, macOS Sequoia, Music 1.6,
no AirPlay targets on the LAN). Time-boxed to ~90 minutes. **Verdict: partial go.** The bridge
ships as an *AirPlay routing helper* with an experimental flag; "one button Pandora Sync" as the
PRD imagined it is not achievable by scripting, because Pandora has no macOS app and no stream URL.

## What was tested locally

| Step | Result |
|---|---|
| `osascript -e 'tell application "Music" to get name of every AirPlay device'` | Works once Music is running (0.6 s). **Timed out at 45 s twice while Music cold-launched.** |
| AppleScript `current AirPlay devices` | Error -1728 (property not readable in this Music build). |
| JXA `Application("Music").airplayDevices()` with `id/name/kind/selected/active/available/soundVolume` | Works, returns clean JSON. Only the `computer` device exists here. |
| JXA `device.selected = true` (per device) | Works, idempotent. |
| JXA `m.currentAirplayDevices = [...]` | Fails: descriptor type mismatch (-10001). Use per-device `selected`. |
| JXA `soundVolume` get/set, `playerState`, `currentTrack` | Work. |
| `open https://www.pandora.com` | Opens the default browser. |
| Automation permission | Terminal → Music was already allowed here; a fresh hub Mac will prompt once in the GUI session. |

The hub's `MacOSAirPlayBridge` runs exactly these JXA scripts (`hub/src/illyhub_hub/airplay.py`)
and was exercised end to end against the local Music app: list, set outputs (select first, then
deselect the rest so Music never drops its last output), volume, now-playing, open Pandora, and
the error classification for permission denied, timeout, and non-macOS.

## What needs the hub Mac

- Real AirPlay targets (the Denon/HEOS receiver and the Sonos players appear to Music as
  `AirPlay device` kinds only when they advertise AirPlay 2 and are on the same LAN).
- **Multi-output selection actually producing sound on both systems** and how tightly they align.
  macOS AirPlay 2 multi-output is designed to be in sync; this is the entire reason the path exists.
- The Automation prompt (System Settings → Privacy & Security → Automation → allow the hub's
  process, which is `uv`/Python launched by launchd, to control Music). Headless launchd sessions
  never see the prompt, so it must be approved once from a Terminal run in the logged-in session,
  and auto-login must be on (FileVault off).
- Cold-launch latency: the bridge uses a 60 s budget when Music is not running, 20 s otherwise.

## The Pandora source decision

Three options were weighed for *what plays* on the Mac:

1. **Pandora web player in a browser, driven by AppleScript/Shortcuts.** Fragile: UI automation of
   a web page, login state, ad breaks, keyboard focus. Not something the hub should own.
2. **Music app playing a Pandora stream.** Impossible: Pandora exposes no stream URL.
3. **Hub routes outputs; a person starts Pandora on the Mac.** Robust and honest: the hub selects
   the AirPlay outputs that match the rooms, opens pandora.com in the Mac's browser, and tells the
   user to press play there. Stop restores the previous output selection.

Option 3 is what shipped. In the app this reads: "Pandora opened on the hub Mac. Press play
there; the rooms follow." A wall tablet cannot press play on the Mac, so this is a feature for the
person sitting at, or screen-sharing into, the hub Mac, or for a household that leaves Pandora
running on the Mac permanently and uses the hub only to route it.

## Go / no-go for the in-app button

- **Go, only from a console-session hub process.** "Pandora Sync (experimental)": one button that
  routes AirPlay outputs to the selected rooms and opens Pandora on the Mac; a Stop that restores.
  Behind `HUB_AIRPLAY_ENABLED=1`, hidden in the app when the hub reports the bridge unavailable.
  The deployed root daemon cannot script Music and says so; the supported path is
  `HUB_AIRPLAY_ENABLED=1 HUB_PORT=8081 uv run hub` as the console user until the helper (illyHub
  #28) exists.
- **No-go for "the hub starts and controls Pandora playback."** Not scriptable without owning a
  browser UI; out of scope for v1 and probably forever.

## Daemon vs. GUI session (found on the hub Mac, September 7 2026)

The hub now runs as a **root LaunchDaemon** (`ops/RUNBOOK.md` §3): on macOS 15 the Local Network
gate denies LAN access to a user LaunchAgent and offers no way to grant it, so the daemon is the
only context that reaches HEOS and Sonos. That collides with this bridge: `osascript` → Music
needs the console user's GUI session, which a system daemon does not have. The bridge therefore
detects `euid == 0` (or no console user on `/dev/console`) and reports
"The hub runs as a system daemon; AirPlay needs the logged-in user's session." without spawning
`osascript`. Auto-login and FileVault-off are **not** required by the hub any more; only this
feature needs a logged-in user, and only while Pandora Sync is in use.

**Follow-up design (not built):** a small user LaunchAgent, `illyhub-airplay-helper`, that runs
in the logged-in session, owns all Music scripting (the same JXA the bridge already has), and
talks to the daemon over `localhost` only. Loopback is not subject to the Local Network gate, so
the helper needs no LAN permission; the daemon needs no GUI. Shape: the helper listens on
`127.0.0.1:<port>` with a shared secret written by the daemon into a root-owned file the helper
can read via a group, exposes `GET /outputs`, `POST /outputs`, `GET /volume`, `POST /volume`,
`POST /open-pandora`; the daemon's `MacOSAirPlayBridge` becomes an HTTP client to it and keeps its
error classification (helper down → "AirPlay needs the logged-in user's session"). The helper is
installed by `ops/install.sh` as `~/Library/LaunchAgents/com.illyhub.airplay-helper.plist` and
approves the Automation prompt once from Terminal. Tracked as illyHub #28.

## Shipped surface

- `GET /api/airplay` → `{enabled, available, reason, outputs[{id, name, kind, selected, active,
  available, volume}]}`
- `POST /api/airplay/outputs {ids}` → Ack (selects exactly those outputs)
- `POST /api/pandora-sync/start {side_ids}` or `{output_ids}` → Ack; with `side_ids` the hub
  matches only those rooms' names to output names (exact, then case-insensitive containment,
  grouped side names split on " + "); unmatched rooms refuse and are named
- `POST /api/pandora-sync/stop` → Ack; restores the previous selection
- `GET /api/pandora-sync` and `HubState.pandora_sync` over the WebSocket
- `/api/settings` → `hub.airplay {enabled, available, reason}`
- Error code `bridge_unavailable` (503) with a plain-sentence reason from the bridge

## Hardware checklist (hub Mac)

1. In the logged-in GUI session, run once from Terminal:
   `osascript -l JavaScript -e 'JSON.stringify(Application("Music").airplayDevices().map(d=>[d.name(),d.kind(),d.available()]))'`
   and approve the Automation prompt. Record the device names and kinds.
2. `HUB_AIRPLAY_ENABLED=1` in `hub/.env`, restart the hub, `GET /api/airplay` → outputs listed.
3. `POST /api/pandora-sync/start` → both rooms selected in Music's AirPlay menu; pandora.com opens.
4. Press play on the Mac. Do both systems play in sync? Note any offset.
5. `POST /api/pandora-sync/stop` → Music's output selection is back to what it was.
6. Until the helper exists, the daemon deployment reports the bridge unavailable by design; the
   checklist above runs the hub from a Terminal in the GUI session
   (`HUB_AIRPLAY_ENABLED=1 uv run hub` on another port) to exercise the scripting.
