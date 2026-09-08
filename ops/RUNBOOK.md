# Hub Mac runbook

The hub runs on the 2019 16" MacBook Pro (Intel i9, macOS Sequoia), lid closed, wired Ethernet, always on.
This file is the executable version of PRD §6. Every item has a command or a settings path.

---

## 0. Information to gather before first install

> **Before anything else:** clone to `~/illyHub`, not into `~/Documents`, `~/Desktop` or
> `~/Downloads`. Those are TCC-protected and a launchd job cannot read them, which makes the hub
> hang with no log output at all. See §3.

Run these on the hub Mac and paste the output into an issue or hand it over. Nothing here is secret
except where marked; redact those.

```bash
# Identity and network
sw_vers; sysctl -n machdep.cpu.brand_string
ifconfig | grep -A3 'en[0-9]:' | grep -E 'en[0-9]|inet '     # which interface is wired, its IP
ipconfig getoption en0 router 2>/dev/null; ipconfig getoption en1 router 2>/dev/null
scutil --dns | grep 'nameserver\[0\]' | head -1

# Devices on the LAN (run for ~5s each). Records IP, model, and name for the static device list.
python3 - <<'PY'
import socket, time
def probe(st):
    m=("M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\nMAN: \"ssdp:discover\"\r\nMX: 2\r\nST: %s\r\n\r\n"%st).encode()
    s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM); s.settimeout(3); s.sendto(m,("239.255.255.250",1900))
    out={}; t=time.time()
    while time.time()-t<4:
        try:
            d,a=s.recvfrom(4096); out[a[0]]=[l for l in d.decode(errors="ignore").split("\r\n") if l.upper().startswith(("LOCATION","SERVER","USN"))]
        except socket.timeout: break
    return out
for n,st in [("HEOS","urn:schemas-denon-com:device:ACT-Denon:1"),("Sonos","urn:schemas-upnp-org:device:ZonePlayer:1")]:
    print(n); [print(" ",ip,*v,sep="\n    ") for ip,v in probe(st).items()]
PY

# Denon: confirm the legacy control API answers (replace IP). Expect "ZMON" or "ZMOFF".
curl -s 'http://DENON_IP/goform/formMainZone_MainZoneXml.xml' | grep -o '<Power><value>[A-Z]*' 
nc -z -w2 DENON_IP 23 && echo "telnet 23 open" || echo "telnet 23 closed/busy"

# HEOS CLI reachable (replace IP). Expect a JSON heos response.
printf 'heos://system/heart_beat\r\n' | nc -w2 HEOS_IP 1255

# Sonos: household and player list (replace IP).
curl -s 'http://SONOS_IP:1400/status/topology' | head -40
curl -s 'http://SONOS_IP:1400/xml/device_description.xml' | grep -E 'roomName|modelName|serialNum' 

# Streaming services linked on Sonos (needed for Tidal/YT Music/Pandora sid + sn values later).
curl -s 'http://SONOS_IP:1400/status/accounts' 
```

Also note, in words:
- Room names as set in the HEOS app and the Sonos app (the hub inherits them, never renames).
- Which Denon zones exist and what each drives (Main, Zone 2).
- Whether Tidal, YouTube Music, and Pandora are linked inside the HEOS app and inside the Sonos app.
- Whether the same Tidal account is used in both. (Sync Play depends on the concurrent-stream test, ai-dev #9.)
- Whether FileVault is on (`fdesetup status`). Auto-login requires it off.
- Tailscale installed? (`tailscale status`)

## 1. Hardware

- [ ] Network. Wi-Fi works and is the current setup; see `docs/mac-hub-kickoff.md` section 6 for the Wi-Fi settings that matter (same SSID/VLAN as the speakers, DHCP reservation, static device list). Wired Ethernet via a USB-C adapter is the upgrade if SSDP or Sonos events prove flaky. With Ethernet in place: `networksetup -setairportpower <wifi-en> off` (find it with `networksetup -listallhardwareports`).
- [ ] Power connected permanently.
- [ ] Shelf placement with airflow, lid closed.
- [ ] Battery check monthly: `system_profiler SPPowerDataType | grep -E 'Condition|Cycle Count'`. Any swelling: stop using the battery, consult a repair shop.

## 2. macOS configuration

- [ ] Energy (MacBook on Sequoia): System Settings → Battery → Options → "Prevent automatic sleeping on power adapter when the display is off" ON, "Wake for network access" ON, "Start up automatically after a power failure" ON. Or, equivalently:
      `sudo pmset -c sleep 0 disksleep 0 displaysleep 10 womp 1 autorestart 1`
- [ ] Static IP or DHCP reservation for the hub Mac on the router. Record it here: `HUB_IP = ________`
- [ ] DHCP reservations for every HEOS, Sonos, and Denon device. Record them in `hub/.env` as `HUB_STATIC_DEVICES`.
- [ ] Remote admin: System Settings → General → Sharing → Remote Login (SSH) ON, Screen Sharing ON.
- [ ] Tailscale: install from tailscale.com (no Homebrew on this Mac), sign in, then `tailscale status`. Later: `tailscale serve --bg 8080` for HTTPS.
- [x] Auto-login: **not needed.** The hub is a root LaunchDaemon and starts at boot with nobody
      logged in (§3), so **FileVault can stay ON**. Only the Phase 7 AirPlay bridge would need a
      logged-in session; deal with that when Phase 7 lands rather than weakening disk encryption now.
- [ ] Manual AirPlay 2 multi-output check (Phase 7 gate): Music app → AirPlay icon → tick both a HEOS device and a Sonos device → play. Note the result: ________
- [ ] `caffeinate` is already in the LaunchDaemon; Amphetamine is optional.

## 3. Service deployment

The hub runs as a **root LaunchDaemon** in `/Library/LaunchDaemons`. This is not a preference:
see "Local Network" below. It also means the hub starts at boot with nobody logged in, so
**FileVault can stay on** and auto-login is not needed (supersedes the note in section 2).

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh          # no Homebrew needed
git clone https://github.com/illycut/illyHub.git ~/illyHub
cd ~/illyHub
cp hub/.env.example hub/.env   # then edit: HUB_HOST, HUB_PORT, HUB_STATIC_DEVICES, HUB_DENON_HOST, HUB_HEOS_HOST, HUB_DATA_DIR
./ops/install.sh               # NOT `sudo ./ops/install.sh` — see below
curl -s 'http://127.0.0.1:8080/api/health'    # with mkcert TLS add -k and use https://
```

- **Run `./ops/install.sh` as yourself, without `sudo`.** It escalates the two steps that need
  root (`launchctl`, writing `/Library`) and prompts you once. Running the whole script under
  `sudo` still works, but `uv sync` then runs as root too and creates a **root-owned
  `hub/.venv`**, after which `pytest` and `uv` need `sudo` as well. On an existing checkout the
  venv already exists so nothing breaks and the mistake is invisible; on a clean clone it bites.
  If it has already happened: `sudo chown -R "$(id -un):staff" hub/.venv`.
- `install.sh` printing `illyHub running: http://<host>.local:8080/api/health` **is** the health
  check passing — it polls that endpoint itself. No need to re-run curl to confirm.

- **Do not clone into `~/Documents`, `~/Desktop` or `~/Downloads`.** Those are TCC-protected and
  a launchd job has no consent grant, so it gets `Operation not permitted` and the hub **hangs
  with nothing in any log** (uv and Python block rather than erroring). `install.sh` now refuses
  to install from those paths. `~/illyHub` is fine.
- **Local Network (Sequoia): only a root daemon reaches the LAN.** macOS 15 gates LAN unicast and
  multicast behind System Settings, Privacy and Security, Local Network. Verified September 7
  2026: a user LaunchAgent is denied, a system daemon with `UserName` set is denied, and there is
  **no way to grant it** — the process never appears in that settings pane, and
  `tccutil reset LocalNetwork com.illyhub.hub` answers `Failed to reset` because no entry exists.
  Wrapping the hub in a signed `.app` bundle and launching it via LaunchServices did not help
  either. Denied traffic surfaces as `EHOSTUNREACH`, so it reads like a network fault:
  `Unable to connect to <ip>: OSError: [Errno 65] No route to host` and `no Sonos players
  discovered`, while the same binary run from Terminal reaches everything. If you see that
  pattern, check that the job is loaded in the **system** context and running as root.
- **`HUB_DATA_DIR` must be an absolute path outside the repo** (`/usr/local/var/illyhub`).
  As root the hub would otherwise scatter root-owned files through the checkout, which then
  breaks running the hub or `pytest` as yourself. Keep it `chmod 700` and `vault.key` `600`:
  the Fernet key sits next to the ciphertext, so directory permissions do the real work.
- Logs: `/Library/Logs/illyhub/hub.log` (JSON, rotated by the hub: 7 files x 10 MB). `launchd.out.log` /
  `launchd.err.log` catch only startup failures before logging is configured.
- Restart: `sudo launchctl kickstart -k system/com.illyhub.hub`, or from the app's Settings
  (`POST /api/hub/restart`): the hub exits with code 0 and `KeepAlive=true` relaunches it
  regardless of exit code. `HUB_ALLOW_RESTART=0` disables the endpoint.
- Request-origin policy: the hub refuses unknown `Host` headers and cross-origin POSTs. Add the
  names phones will use to `hub/.env`: `HUB_ALLOWED_HOSTS=["hub-mac.<tailnet>.ts.net"]` and, for
  HTTPS via Tailscale or mkcert, `HUB_ALLOWED_ORIGINS=["https://hub-mac.<tailnet>.ts.net"]`.
  `localhost`, `127.0.0.1`, the LAN IP and `*.local` are always allowed. A 400 "Invalid host
  header" or a 403 `forbidden_origin` in the app means a name is missing here.
- Stop: `sudo launchctl bootout system/com.illyhub.hub`
- Update, by hand: `git pull && ./ops/install.sh`.
- Update, from the app (Phase 8, `Settings → Hub → Check for updates`). **Off unless you opted
  in**: `install.sh` asks once ("Allow in-app self-update?") and writes `HUB_ALLOW_UPDATE=0|1`
  to `hub/.env`; the endpoint runs git and the dependency sync on this Mac, gated only by the LAN,
  so the default is no. When on: `GET /api/hub/update/check` runs `git fetch` in the checkout;
  `POST /api/hub/update/apply` (X-Illyhub header) starts `ops/update.sh` detached (log:
  `$HUB_DATA_DIR/update/<job>.log`, state file next to it, last 10 jobs kept). The script refuses
  a dirty or detached checkout and a remote that is not `https://`/`ssh://`, fast-forwards, runs
  `uv sync` and `npm ci && npm run build` **as the checkout's owner** (not root), then the hub
  exits and launchd relaunches it on the new code. A refused merge (local commits, uncommitted
  edits) leaves the checkout untouched and reports `failed` with the reason; a failure after the
  merge resets to the previous commit and re-syncs (`rolled_back`); if even that fails the state
  is `failed` and the hub keeps running the old code. `up_to_date` means nothing changed and the
  hub stays up. A `running` job whose process is gone for 15 minutes is reported `failed`
  ("run ops/update.sh by hand").
  **PATH:** the daemon's PATH is fixed by the plist. `install.sh` resolves `uv`, `node` and
  `npm` at install time and bakes their directories in, so re-run `./ops/install.sh` after
  installing or moving Node (nvm) or uv; otherwise the updater reports `uv not found on PATH`
  or skips the PWA build.
- Metrics (Phase 8): `curl -s http://127.0.0.1:8080/api/metrics/summary` for the household
  sentence ("Running since 7:37 am today. 10 commands, all worked."); `GET /api/metrics` for the
  numbers (uptime %, transport/volume p95, reconnects, Sync Play start delta and drift, peak
  clients) for today plus 30 daily rollups
  (`$HUB_DATA_DIR/metrics.sqlite`). The summary is also logged once a day in `hub.log`.
- Uninstall: `./ops/install.sh --uninstall`

## 3a. HTTPS for the PWA

Service workers (offline shell, Android install prompt) require a secure origin. Plain
`http://<hub-ip>:8080` still works for a bookmark-style install, but pick one of these.

**Default: Tailscale (no certificates to manage).**
Prerequisite, once, in the Tailscale admin console (DNS page): enable **MagicDNS** and
**HTTPS Certificates**. Without both, `tailscale serve` cannot mint a certificate.
```bash
tailscale up                              # once, sign in
tailscale serve --bg 8080                 # HTTPS on https://<hub-name>.<tailnet>.ts.net
tailscale serve status
```
The hub keeps serving plain HTTP on 8080; Tailscale terminates TLS with a MagicDNS certificate.
Works on the LAN for devices that are on the tailnet and from anywhere remote. Leave
`HUB_TLS_CERT`/`HUB_TLS_KEY` empty.

**Alternative: mkcert (LAN-only devices that are not on the tailnet).**
```bash
brew install mkcert nss
mkcert -install                           # trusts a local CA on the hub Mac
mkdir -p ~/illyHub/hub/tls                # gitignored; the hub resolves tls/ relative to hub/
mkcert -cert-file ~/illyHub/hub/tls/hub.pem -key-file ~/illyHub/hub/tls/hub-key.pem \
  "$(hostname -s).local" 192.168.1.10     # the hub's .local name and reserved IP
```
Then in `hub/.env`: `HUB_TLS_CERT=tls/hub.pem`, `HUB_TLS_KEY=tls/hub-key.pem` (relative to
`hub/`; the hub refuses to start if either file is missing), and `./ops/install.sh`. The hub
now serves `https://<hostname>.local:8080`, and every health check below becomes
`curl -sk https://127.0.0.1:8080/api/health` (`-k` because the CA is local). `install.sh`
detects `HUB_TLS_CERT` in `.env` and switches scheme automatically. Install the
root CA on each phone: `mkcert -CAROOT` prints the folder; AirDrop `rootCA.pem` to the phone.
iOS: Settings → General → VPN & Device Management → install, then Settings → General → About →
Certificate Trust Settings → enable. Android: Settings → Security → Install a certificate → CA.

**Install the app on a phone**
- iOS (Safari): open the hub URL → Share → *Add to Home Screen*. Standalone mode works even on
  plain HTTP; the service worker only registers on HTTPS.
- Android (Chrome): open the hub URL → menu → *Install app* (requires HTTPS), or *Add to Home
  screen* on plain HTTP.
- The app export must be built and present at `HUB_APP_DIR` (`app/out`); `/api/health` still
  answers when it is missing, but `/` returns 404 and the hub log says "app export missing".

## 3b. Toolchain constraints on the hub Mac (Intel)

The hub Mac is the 2019 Intel MacBook Pro from PRD 6.1. A dev machine on Apple Silicon will not
hit either of these, so they surface only at deploy time.

- **`cryptography` must stay below 43.** Releases from 43 onward publish arm64-only macOS
  wheels. On Intel, `uv sync` falls back to a source build and fails needing OpenSSL 3 and Rust.
  `pyproject.toml` pins `>=42,<43`; 42.0.8 is the newest with a CPython x86_64 macOS wheel, and
  being `cp37-abi3` it also works on 3.12 through 3.14. Only `Fernet` is used
  (`hub/src/illyhub_hub/auth/vault.py`), so nothing is lost. Do not "upgrade" this pin without
  checking wheels for `macosx_*_x86_64` on PyPI.
- **Node 20+ is required only to build the PWA** (`next` 16, `react` 19, `vitest` 4). The hub
  itself is pure Python and needs no Node at all: if your client is a separate mobile or native
  app hitting the REST and WebSocket API, skip this entirely and expect
  `/api/health` → `static.app: false` (see section 4 item 9). A stock Intel Mac may still have
  Node 16. `app/package.json` declares `engines.node >=20.9` and `app/.npmrc`
  sets `engine-strict=true`, so `npm install` now stops with `EBADENGINE` instead of failing
  deep inside a Next build. No Homebrew needed: install Node 22 from the official pkg at
  nodejs.org, or use `nvm`/`fnm`. Then `cd app && npm ci && npm run build`. Until the export
  exists, `/api/health` shows `static.app: false` and the hub serves the API only.
- Homebrew is not required anywhere: `uv` installs via
  `curl -LsSf https://astral.sh/uv/install.sh | sh` into `~/.local/bin`.

## 4. Recovery: "the Mac rebooted and nothing started"

1. No login is needed: the daemon runs in the system context and starts at boot. If nothing is
   listening, go straight to step 2 rather than looking for a login window.
2. `sudo launchctl print system/com.illyhub.hub | grep -E 'state|last exit'`
3. `sudo tail -50 /Library/Logs/illyhub/hub.log; sudo tail -20 /Library/Logs/illyhub/launchd.err.log`
4. Common causes: `.venv` missing or moved → rerun `./ops/install.sh`; `.env` missing → copy from
   example; port in use → `sudo lsof -nP -iTCP:8080 -sTCP:LISTEN` (needs sudo to see a root process).
5. **Nothing in any log and the process is alive but idle:** the checkout is somewhere a launchd
   job cannot read (§3, TCC). Move it to `~/illyHub`.
6. Discovery finds nothing, `[Errno 65] No route to host`: the job is not running as root in the
   system context (§3, Local Network). Confirm with
   `ps -o user,command -p $(sudo launchctl print system/com.illyhub.hub | awk '/pid = /{print $3}')`
   — it must say `root`. A user LaunchAgent will never work. Then `ping <sonos_ip>`; with static
   devices configured the hub comes up regardless. Check IGMP snooping on the switch if multicast
   never works.
7. **`{"code": "not_found", "message": "No route for /api/health\u00a0"}`** — note the
   `\u00a0` in the echoed path. That is a **non-breaking space**: the URL was pasted from a
   rendered document, chat window or web page that turned a space into U+00A0, so the request
   really did ask for a path that does not exist. The hub is fine. Retype the command, or quote
   the URL (`curl -s 'http://127.0.0.1:8080/api/health'`). To confirm what was actually sent:
   `curl -sv 'http://…' 2>&1 | grep '^> GET'` — an NBSP shows up as `%C2%A0`. Nothing in this
   repo contains NBSPs; they always come from the copy.
8. **`Bootstrap failed: 5: Input/output error` from `install.sh`, and the hub is now down.**
   `bootout` returns before the job is really gone (the hub still has to close the HEOS socket,
   the Sonos subscriptions and the Denon telnet link), so a bootstrap that follows immediately
   hits a still-registered label. The bootout *did* work, so the old daemon is gone and the new
   one never loaded — nothing is listening. `install.sh` now waits for the label to disappear and
   retries once, but if you see it: `sudo launchctl bootstrap system /Library/LaunchDaemons/com.illyhub.hub.plist`.
9. `/api/health` shows `fake_devices: true`: `hub/.env` still has `HUB_FAKE_DEVICES=1`. Remove it and kickstart.
10. `/` returns 404 but `/api/health` works, and `/api/health` reports `static.app: false`: there
   is no PWA export at `HUB_APP_DIR`. **This is normal and needs no fix if your client is not the
   PWA** — a separate mobile or native app talks to the REST and WebSocket API and never asks the
   hub for a page. The hub is API-only in that mode and healthy (`status: ok`). Only build the app
   (`cd app && npm ci && npm run build`, needs Node 20+) if you want the hub to serve the PWA.
11. Artwork shows grey squares: `/api/art/...` is answering with `X-Art-Fallback: upstream_error` (200 + placeholder). Check `hub.log` for "art fetch failed"; the device URL (Sonos `/getaa?...`, HEOS CDN) must be reachable from the hub Mac. `X-Art-Fallback: proxy_disabled` means `HUB_ART_PROXY=0` is set.
12. Disk: the art cache is capped at `HUB_ART_MAX_MB` (500) and swept daily; `/api/health` → `art_cache.bytes` shows current usage.

## 5. Hardware verification status

### Verified on the LAN (September 7 2026)

Devices: Denon **AVR-X3400H** at `192.168.50.40` (HEOS player pid 1400399113, firmware
3.139.173 / Aios 4.025, main zone + Zone 2) and a **Sonos Amp** "Outside" at `192.168.50.224`
(`RINCON_C438758063E101400`), household `Sonos_Yf3PogWRdPw6igHxVRlDMMPjEC`. Services linked on
HEOS: Tidal and Pandora. YouTube Music is linked on Sonos, not on HEOS.

- [x] `/api/health` shows heos, sonos and denon all `connected` (as the root daemon).
- [x] `/api/devices` lists both players and both Denon zones with the vendor room names.
- [x] Sonos transport: `play` resumes at the stored position, `pause`, and `seek` restores an
      exact position on a YouTube Music HLS stream (`x-sonosapi-hls-static`, `sid=284`).
- [x] Sonos state follows the device over UPnP events: an external volume change reached hub
      state, and `pause` was reflected in 0.21 s.
- [x] Denon zone power, volume and mute over telnet, including the read-back behaviour.
- [x] HEOS transport: `set_play_state=play` started the Pandora station on the receiver.

### Known-bad, found on hardware

- **HEOS cannot set volume on the AVR.** `heos://player/set_volume` returns `success` and applies
  nothing; `get_volume` reports `0` regardless of the real `MV`. Volume and mute for that player
  go through the Denon telnet link instead, and the zone's level mirrors onto the player.
  Verified through the REST API on hardware: `POST /api/volume {player, 80}` moved the receiver
  to `MV48` and updated both zone and player.
- **The hub scale is coarser than the receiver's.** Hub 0-100 maps onto `MV 0-HUB_DENON_MAX_VOLUME`
  (60 by default, so 61 steps): asking for 96 lands on `MV58` and reads back 97. Expect a
  slider to settle a point or two from where it was dropped. Linked volume records the settled
  level, not the request, so this is not mistaken for someone turning the knob.
- **`/goform/` is gone on this firmware.** Every legacy HTTP endpoint answers 403 (nginx on
  80/443). Telnet is the only control path here.
- **`Z2OFF` can standby the whole unit**, taking the main zone with it. Power commands read back.
- **`MVMAX` is not stable** (75, 78, 79, 82, 665, 785 observed) and must not feed the volume
  scale; `HUB_DENON_MAX_VOLUME` is a hard cap the receiver cannot raise.
- **Zone 2 feeds an external amp** and is configured as volume-incapable
  (`HUB_DENON_FIXED_ZONES=["zone2"]`). Zone 2 mute is still unverified: the receiver stays silent
  for Zone 2 commands while Zone 2 is off, so it needs Zone 2 powered to test.
- ~~Play state has an unmapped transient.~~ **Fixed.** Sonos reports `TRANSITIONING` for ~1 s
  while a stream buffers and HEOS reports `state=unknown`; both used to fall through to
  `"unknown"`, which is why a play took 1.7-1.9 s to confirm while a pause took 0.21 s. Both
  adapters now hold the last known state across a transient report instead of blanking it, so
  the client's optimistic play survives.
- **Position reports are whole-second and drift against the wall clock** (`1:33:01` held for two
  samples, then `1:33:04 → 1:33:06`), confirming the review 1.2 concern: the 300 ms Sync Play
  threshold needs tick-edge interpolation, not raw position deltas.

### Still to verify

- [ ] Change track in the HEOS app; hub now-playing follows.
- ~~Zone 2 mute.~~ Not applicable: Zone 2 is a fixed pre-out to an external amp, confirmed by
      the HEOS app offering it as power on/off only with no volume and no separate playback.
      `HUB_DENON_FIXED_ZONES=["zone2"]` makes the hub report it power-only.
- [ ] Ai-dev #9 concurrent-stream test and #10 Tidal ref spike. Note for #9: HEOS Tidal is
      `norman@umich.edu` and Sonos was playing YouTube Music, so check which account each
      ecosystem uses before assuming the one-stream limit applies.

### Pandora (Phase 5) on the LAN

Stations are browsed and played per ecosystem; nothing has touched hardware. Checklist and the
assumed tree shapes live in `docs/spikes/pandora-refs.md`. Short form:

- [ ] Both vendor apps are linked to Pandora. `GET /api/settings` shows `pandora.linked: true`.
- [ ] `GET /api/browse/pandora/stations` lists every station from both apps once, with the right
      `availability`, and `linked: {heos: true, sonos: true}`. A `false` with a `last_error` on the
      Pandora settings row is an auth fault (sign in again in that app); a `false` without one is
      not linked (HEOS: `get_music_sources`; Sonos: account serial type 60423, or set
      `HUB_SONOS_PANDORA_SN`); `null` is a hub-side error worth an issue. Table in
      `docs/spikes/pandora-refs.md`.
- [ ] Play a station on the HEOS side, then on the Sonos side, via `POST /api/play`. `next` skips;
      `prev` and seek are refused (409). Note whether Pandora pauses the first room when the second
      starts (the ack carries a `pandora_concurrent` warning either way).
- [ ] Record results on ai-dev #61 / #62 and correct the spike doc.

### YouTube Music (Phase 6) on the LAN

- [ ] Create the Google OAuth client (TV and Limited Input type, YouTube Data API v3 enabled) and
      put its id/secret in `hub/.env` (`HUB_YTMUSIC_CLIENT_ID`, `HUB_YTMUSIC_CLIENT_SECRET`);
      restart. Settings → Accounts → YouTube Music must no longer show the "needs a Google
      OAuth client" line.
- [ ] Link from the app: code + google.com/device → `GET /api/auth/ytmusic/status` reaches
      `linked` with your account name.
- [ ] `curl -s http://SONOS_IP:1400/status/accounts | grep 72711` shows a serial; browse items
      report `availability.sonos: true`. If not, set `HUB_SONOS_YTMUSIC_SN`.
- [ ] Play an album on a Sonos room from the app. Silence or "unable to play" means the URI
      template is wrong: follow docs/spikes/ytmusic-sonos.md (try `HUB_SONOS_YTMUSIC_URI`
      alternatives) and record what worked in an illyHub issue.
- [ ] A HEOS room is disabled in the picker with "YouTube Music isn't available on HEOS."
      (expected; docs/spikes/ytmusic-heos.md).

### Sync Play (Phase 4) on the LAN

Sync Play has only ever run against the fakes. First real run, in this order:

- [ ] ai-dev #9 first: play the same Tidal album on HEOS (HEOS app) and Sonos (Sonos app) at once.
      If one pauses the other, Tidal is enforcing one stream per account and Sync Play needs a
      second account (family plan), one per ecosystem. Record the result on the issue.
- [ ] `curl -X POST http://127.0.0.1:8080/api/sync/play -H 'content-type: application/json' \
        -d '{"content_ref":{"service":"tidal","kind":"album","id":"<id from /api/home>"},"heos_target":"<heos player id>","sonos_target":"<sonos player id>"}'`
      Expect an ack with `applied` listing both sides and `GET /api/sync` moving through
      `priming → verifying → starting → locked` within ~10 s. Listen in one room at a time first.
- [ ] Note `start_delta_ms` from `GET /api/sync` (target: under 200 ms) and watch `drift_ms` for a
      full track (target: held under 300 ms; corrections show as `corrections` climbing slowly).
- [ ] Let a track boundary pass. Both sides should move to the next track; if Sonos lags more than
      3 s the hub re-primes it (log line "sync track boundary" then "sync ended"/"locked").
- [ ] `GET /api/sync/sessions/<id>/report` after `POST /api/sync/stop`. Paste the report and the
      CSV from `~/illyHub/hub/data/sync/` into an illyHub issue for tuning.
- [ ] If drift oscillates: raise `HUB_SYNC_CORRECTION_GAP_S` or lower `HUB_SYNC_LOOKAHEAD_MS` via
      `POST /api/sync/config` (no restart). If the follower starts late every time, raise the
      lookahead. If `verifying` fails (`sync_mismatch`), the HEOS or Sonos queue did not load the
      expected track: check `docs/spikes/tidal-refs.md` (ai-dev #10).

### Queue, play mode, search, self-update (Phase 8) on the LAN

Mock-verified only. First real run:

1. Play an album on the receiver, then `curl -s http://127.0.0.1:8080/api/queue/heos:heos-<pid> | python3 -m json.tool`:
   items should list the album with `current_index` matching the playing track. Add a track from
   the HEOS app; the hub should re-read within a second (`queues.<side>` delta on `/ws`).
2. `POST /api/queue/jump {"target": "heos-<pid>", "index": 3}` → the receiver jumps to track 4
   (HEOS queue ids are 1-based; the hub maps from the read). Same on the Sonos Amp.
3. `POST /api/playmode {"target": "<side>", "shuffle": true}` → the vendor app shows shuffle on;
   toggle repeat in the vendor app → `play_mode` follows on `/ws`.
4. `GET /api/search?q=<something in your library>` → albums/playlists/tracks from Tidal and
   YouTube Music, stations from Pandora; `errors` names anything that timed out.
5. `GET /api/hub/update/check` → `available` true when the hub Mac is behind `origin/main`;
   `POST /api/hub/update/apply -H 'X-Illyhub: 1'` → status `running`, then the hub restarts on
   the new commit (or `rolled_back` with the reason). Check `/api/health` `version`/uptime after.

### AirPlay bridge / Pandora Sync (Phase 7, experimental) on the LAN

Nothing here has touched hardware; the dev Mac only proved the JXA scripts run against Music
(`docs/spikes/airplay-bridge.md`). **The production hub is a root LaunchDaemon (§3) and cannot
script Music from there**: `GET /api/airplay` reports "The hub runs as a system daemon; AirPlay
needs the logged-in user's session." by design. The follow-up is a user-session helper
(`illyhub-airplay-helper`, illyHub #28, spike doc → "Daemon vs. GUI session"); until it exists, exercise the
bridge from a Terminal in the logged-in session on a second port. Auto-login and FileVault-off are
**not** required for the hub; a logged-in user is needed only while Pandora Sync is used.

1. In the logged-in GUI session, approve Automation once from Terminal:
   `osascript -l JavaScript -e 'JSON.stringify(Application("Music").airplayDevices().map(d=>[d.name(),d.kind(),d.available()]))'`
   Allow "Terminal wants to control Music". Record the device names and kinds.
2. From that Terminal: `cd ~/illyHub/hub && HUB_AIRPLAY_ENABLED=1 HUB_PORT=8081 uv run hub`
   (the daemon keeps :8080). `curl -s http://127.0.0.1:8081/api/airplay | python3 -m json.tool`
   → `available: true` and the receiver / Sonos players listed as AirPlay outputs. A permission
   reason means step 1 was skipped; a timeout means Music could not open.
3. `curl -X POST -H 'X-Illyhub: 1' -H 'content-type: application/json' -d '{"side_ids":["<heos side id>","<sonos side id>"]}' http://127.0.0.1:8081/api/pandora-sync/start` → Music's
   AirPlay menu shows both rooms selected and pandora.com opens. Press play on the Mac. Do both
   systems play together? Note any offset.
4. `curl -X POST -H 'X-Illyhub: 1' http://127.0.0.1:8081/api/pandora-sync/stop` → the previous
   output selection is back.
5. Record the device kinds, the sync offset, and whether the helper design is worth building.
