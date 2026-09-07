# Hub Mac kickoff

First-time setup of the hub on the always-on Mac, written after Phase 0 landed
(`4bb0b45`, September 7, 2026). The full reference is `ops/RUNBOOK.md`; this is the
short path to a first LAN run. Do the steps in order.

## 1. Install prerequisites and clone

```bash
brew install uv git
git clone https://github.com/illycut/illyHub.git ~/illyHub
```

## 2. Gather device information

Run every command in `ops/RUNBOOK.md` **section 0** on the hub Mac and paste the output
back into the dev session (or into an issue on `illycut/illyHub`). It yields:

- device IPs and models for HEOS, Sonos, and the Denon receiver
- room names as set in the vendor apps (the hub inherits them, never renames)
- which Denon zones exist
- which streaming services are linked in the HEOS app and in the Sonos app
- FileVault status (auto-login requires it off)
- whether Tailscale is installed

With that output the static device list for `.env` can be written for you.

## 3. Create `hub/.env`

```bash
cp ~/illyHub/hub/.env.example ~/illyHub/hub/.env
```

Edit it:

- `HUB_HEOS_HOST` and `HUB_DENON_HOST` = the receiver's IP
- `HUB_STATIC_DEVICES` = JSON list from step 2, or leave `[]` to rely on SSDP
- Leave `HUB_FAKE_DEVICES` unset. If it is set, the hub serves invented players and
  `install.sh` will warn.

## 4. Install the service

```bash
cd ~/illyHub && ./ops/install.sh
```

Then the one-time **Local Network permission** step (macOS Sequoia gates multicast and
UPnP callbacks; a launchd-started process never gets the prompt):

```bash
cd ~/illyHub/hub && uv run hub      # allow the Local Network prompt, then Ctrl-C
launchctl kickstart -k gui/$(id -u)/com.illyhub.hub
```

Confirm the hub entry is enabled under System Settings → Privacy & Security → Local Network.
Skipping this step looks exactly like broken discovery.

## 5. First LAN run checklist

Work through `ops/RUNBOOK.md` **section 5** and report anything that fails:

```bash
curl -s http://127.0.0.1:8080/api/health  | python3 -m json.tool
curl -s http://127.0.0.1:8080/api/devices | python3 -m json.tool
```

- `/api/health` shows heos, sonos, denon all `connected`, `fake_devices: false`
- `/api/devices` lists every player and both Denon zones with the right room names
- Change volume in the Sonos app; hub state follows within 1s
- Change track in the HEOS app; hub now-playing follows
- Zone power on/off via the hub turns the Denon on and off (Phase 1 endpoint)

This is the first time the HEOS, Sonos, and Denon adapters touch real devices. Everything
up to here was verified only against mocks and protocol fakes, so findings are expected.

## 6. Any time, not blocking

`ops/RUNBOOK.md` sections 1 and 2: wired Ethernet, energy settings, DHCP reservations for
the hub and every device, SSH and Screen Sharing, Tailscale, auto-login.

## Later phases

Every later phase is:

```bash
cd ~/illyHub && git pull && ./ops/install.sh
```

Open spikes that need this machine: ai-dev #9 (Tidal and YouTube Music concurrent-stream
check, gates Sync Play) and #10 (Tidal ID to HEOS and Sonos playable refs).
