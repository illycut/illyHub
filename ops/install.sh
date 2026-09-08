#!/usr/bin/env bash
# Install or update the illyHub service on the hub Mac. Idempotent.
#
#   ./ops/install.sh            install/refresh the daemon and (re)start the hub
#   ./ops/install.sh --uninstall
#
# Assumes: uv installed, repo cloned outside ~/Documents (see below), hub/.env present.
#
# The hub is a **root LaunchDaemon**, not a user LaunchAgent. macOS Sequoia's Local Network
# gate denies LAN access to a LaunchAgent and to a daemon with UserName set, and offers no way
# to grant it, so discovery silently finds nothing (EHOSTUNREACH / "no Sonos players
# discovered"). Only the system context reaches the LAN. Details in ops/RUNBOOK.md section 3.
#
# `uv sync` deliberately runs as the invoking user so .venv stays user-owned and developers can
# still run pytest without sudo; only the launchctl and /Library steps use sudo.
set -euo pipefail

LABEL="com.illyhub.hub"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PLIST_SRC="$REPO/ops/$LABEL.daemon.plist"
PLIST_DST="/Library/LaunchDaemons/$LABEL.plist"
LOGDIR="/Library/Logs/illyhub"
LEGACY_AGENT="$HOME/Library/LaunchAgents/$LABEL.plist"
UV="$(command -v uv || true)"

SUDO="sudo"
[[ "$(id -u)" -eq 0 ]] && SUDO=""

if [[ "${1:-}" == "--uninstall" ]]; then
  $SUDO launchctl bootout "system/$LABEL" 2>/dev/null || true
  $SUDO rm -f "$PLIST_DST"
  echo "Removed $LABEL. Logs left in $LOGDIR, data left in HUB_DATA_DIR."
  exit 0
fi

[[ -n "$UV" ]] || {
  echo "uv not found. Install it: curl -LsSf https://astral.sh/uv/install.sh | sh"
  exit 1
}
[[ -f "$REPO/hub/.env" ]] || { echo "Missing $REPO/hub/.env (copy hub/.env.example)"; exit 1; }

# A launchd job cannot read ~/Documents, ~/Desktop or ~/Downloads: those are TCC-protected and
# a daemon has no consent grant, so the hub hangs with no log output at all. Fail loudly here
# instead of leaving someone to debug a silent hang.
case "$REPO/" in
  "$HOME"/Documents/* | "$HOME"/Desktop/* | "$HOME"/Downloads/*)
    echo "ERROR: the checkout is under a TCC-protected folder:"
    echo "  $REPO"
    echo "A launchd job cannot read it (Operation not permitted) and the hub will hang"
    echo "silently, with nothing in any log. Move the checkout, e.g. to ~/illyHub, and re-run."
    exit 1
    ;;
esac

# Sync dependencies as the invoking user so .venv does not become root-owned.
(cd "$REPO/hub" && { "$UV" sync --frozen 2>/dev/null || "$UV" sync; })

[[ -x "$REPO/hub/.venv/bin/hub" ]] || { echo "uv sync did not produce hub/.venv/bin/hub"; exit 1; }

$SUDO mkdir -p "$LOGDIR"

# Render to a temp file and lint before installing so a bad render never lands in LaunchDaemons.
TMP_PLIST="$(mktemp)"
sed -e "s|__REPO__|$REPO|g" -e "s|__LOGDIR__|$LOGDIR|g" "$PLIST_SRC" > "$TMP_PLIST"
plutil -lint "$TMP_PLIST" >/dev/null
$SUDO install -o root -g wheel -m 644 "$TMP_PLIST" "$PLIST_DST"
rm -f "$TMP_PLIST"

# Retire the pre-Sequoia user agent if it is still around; it cannot reach the LAN and would
# fight the daemon for port 8080.
if [[ -f "$LEGACY_AGENT" ]]; then
  launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
  rm -f "$LEGACY_AGENT"
  echo "Removed the legacy user LaunchAgent (it cannot reach the LAN on Sequoia)."
fi

# Refuse to silently run invented devices on the real hub.
if grep -Eq '^HUB_FAKE_DEVICES=(1|true|True)' "$REPO/hub/.env"; then
  echo "WARNING: hub/.env sets HUB_FAKE_DEVICES. The hub will serve fake players, not your hardware."
fi

# Warn when data would land in the repo: as a root daemon the hub would scatter root-owned
# files through the checkout, which then breaks running the hub or tests as yourself.
if ! grep -Eq '^HUB_DATA_DIR=/' "$REPO/hub/.env"; then
  echo "WARNING: HUB_DATA_DIR is not an absolute path. The root daemon will write root-owned"
  echo "         files under $REPO/hub. Set HUB_DATA_DIR=/usr/local/var/illyhub in hub/.env."
fi

# Restart cleanly whether or not it was already loaded.
$SUDO launchctl bootout "system/$LABEL" 2>/dev/null || true
$SUDO launchctl bootstrap system "$PLIST_DST"

# Cold start takes a few seconds (interpreter, adapters, SSDP window), so poll instead of
# sleeping once: a single short sleep reported a false failure on every first install.
PORT="$(grep -E '^HUB_PORT=' "$REPO/hub/.env" | cut -d= -f2 || true)"
PORT="${PORT:-8080}"
for _ in $(seq 1 30); do
  if curl -fsS "http://127.0.0.1:$PORT/api/health" >/dev/null 2>&1; then
    echo "illyHub running: http://$(hostname -s).local:$PORT/api/health"
    exit 0
  fi
  sleep 1
done
echo "Daemon loaded but health check failed after 30s."
echo "Check $LOGDIR/hub.log and $LOGDIR/launchd.err.log"
exit 1
