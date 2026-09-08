# Hub Mac runbook

The hub runs on the 2019 16" MacBook Pro (Intel i9, macOS Sequoia), lid closed, wired Ethernet, always on.
This file is the executable version of PRD §6. Every item has a command or a settings path.

---

## 0. Information to gather before first install

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
- [ ] Tailscale: `brew install --cask tailscale`, sign in, then `tailscale status`. Later: `tailscale serve --bg 8080` for HTTPS.
- [ ] Auto-login: System Settings → Users & Groups → Automatically log in as → hub user. **Requires FileVault OFF** (`sudo fdesetup disable`). Decide and record: FileVault ________
- [ ] Manual AirPlay 2 multi-output check (Phase 7 gate): Music app → AirPlay icon → tick both a HEOS device and a Sonos device → play. Note the result: ________
- [ ] `caffeinate` is already in the LaunchAgent; Amphetamine is optional.

## 3. Service deployment

```bash
brew install uv git
git clone https://github.com/illycut/illyHub.git ~/illyHub
cd ~/illyHub
cp hub/.env.example hub/.env   # then edit: HUB_HOST, HUB_PORT, HUB_STATIC_DEVICES, HUB_DENON_HOST, HUB_HEOS_HOST
./ops/install.sh
curl -s http://127.0.0.1:8080/api/health | python3 -m json.tool   # with mkcert TLS: curl -sk https://127.0.0.1:8080/api/health
```

- **Local Network permission (Sequoia, do this once):** macOS 15 gates LAN multicast and UPnP callbacks behind
  System Settings → Privacy & Security → Local Network. A launchd-started process in a headless session never
  gets the prompt, so discovery finds nothing and it looks like broken SSDP. After the first `./ops/install.sh`,
  run `cd ~/illyHub/hub && uv run hub` once from Terminal to trigger the prompt, allow it, Ctrl-C, then
  `launchctl kickstart -k gui/$(id -u)/com.illyhub.hub`. Confirm the entry is enabled in that settings pane.
- Logs: `~/Library/Logs/illyhub/hub.log` (JSON, rotated by the hub: 7 files x 10 MB). `launchd.out.log` /
  `launchd.err.log` catch only startup failures before logging is configured.
- Restart: `launchctl kickstart -k gui/$(id -u)/com.illyhub.hub`, or from the app's Settings
  (`POST /api/hub/restart`): the hub exits with code 0 and `KeepAlive=true` relaunches it
  regardless of exit code. `HUB_ALLOW_RESTART=0` disables the endpoint.
- Request-origin policy: the hub refuses unknown `Host` headers and cross-origin POSTs. Add the
  names phones will use to `hub/.env`: `HUB_ALLOWED_HOSTS=["hub-mac.<tailnet>.ts.net"]` and, for
  HTTPS via Tailscale or mkcert, `HUB_ALLOWED_ORIGINS=["https://hub-mac.<tailnet>.ts.net"]`.
  `localhost`, `127.0.0.1`, the LAN IP and `*.local` are always allowed. A 400 "Invalid host
  header" or a 403 `forbidden_origin` in the app means a name is missing here.
- Stop: `launchctl bootout gui/$(id -u)/com.illyhub.hub`
- Update: `git pull && ./ops/install.sh`
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

## 4. Recovery: "the Mac rebooted and nothing started"

1. Did the user session log in? If the login window is showing, auto-login is off (see FileVault above). Log in once; the agent starts with the session.
2. `launchctl print gui/$(id -u)/com.illyhub.hub | grep -E 'state|last exit'`
3. `tail -50 ~/Library/Logs/illyhub/hub.log; tail -20 ~/Library/Logs/illyhub/launchd.err.log`
4. Common causes: `uv` moved (Homebrew path changed) → rerun `./ops/install.sh`; `.env` missing → copy from example; port in use → `lsof -i :8080`.
5. Discovery finds nothing: first check Local Network permission (§3). Then `ping <sonos_ip>`; with static devices configured the hub comes up regardless. Check IGMP snooping on the switch if multicast never works.
6. `/api/health` shows `fake_devices: true`: `hub/.env` still has `HUB_FAKE_DEVICES=1`. Remove it and kickstart.
7. `/` returns 404 but `/api/health` works: the PWA export is not at `HUB_APP_DIR`. Build the app (`cd app && npm run build`) or fix the path, then kickstart.
8. Artwork shows grey squares: `/api/art/...` is answering with `X-Art-Fallback: upstream_error` (200 + placeholder). Check `hub.log` for "art fetch failed"; the device URL (Sonos `/getaa?...`, HEOS CDN) must be reachable from the hub Mac. `X-Art-Fallback: proxy_disabled` means `HUB_ART_PROXY=0` is set.
9. Disk: the art cache is capped at `HUB_ART_MAX_MB` (500) and swept daily; `/api/health` → `art_cache.bytes` shows current usage.

## 5. What is verified only against fakes (as of Phase 0/1)

Until the hub runs on this Mac, every adapter has been exercised only against mocks and the protocol fakes.
First LAN run checklist:
- [ ] `/api/health` shows heos, sonos, denon all `connected`.
- [ ] `/api/devices` lists every player and both Denon zones with the right room names.
- [ ] Change volume in the Sonos app; hub state follows within 1s (WebSocket or repeated GET).
- [ ] Change track in the HEOS app; hub now-playing follows.
- [ ] `ZMON`/`ZMOFF` via the hub turns the Denon on and off.
- [ ] Ai-dev #9 concurrent-stream test and #10 Tidal ref spike.

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
