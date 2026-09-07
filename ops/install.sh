#!/usr/bin/env bash
# Install or update the illyHub LaunchAgent on the hub Mac. Idempotent.
#
#   ./ops/install.sh            install/refresh the agent and (re)start the hub
#   ./ops/install.sh --uninstall
#
# Assumes: uv installed (brew install uv), repo cloned, hub/.env present.
set -euo pipefail

LABEL="com.illyhub.hub"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PLIST_SRC="$REPO/ops/$LABEL.plist"
PLIST_DST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOGDIR="$HOME/Library/Logs/illyhub"
UV="$(command -v uv || true)"
UID_NUM="$(id -u)"

if [[ "${1:-}" == "--uninstall" ]]; then
  launchctl bootout "gui/$UID_NUM/$LABEL" 2>/dev/null || true
  rm -f "$PLIST_DST"
  echo "Removed $LABEL. Logs left in $LOGDIR."
  exit 0
fi

[[ -n "$UV" ]] || { echo "uv not found. brew install uv"; exit 1; }
[[ -f "$REPO/hub/.env" ]] || { echo "Missing $REPO/hub/.env (copy hub/.env.example)"; exit 1; }

mkdir -p "$LOGDIR" "$HOME/Library/LaunchAgents"

# Sync dependencies so the first launch does not resolve packages under launchd.
(cd "$REPO/hub" && "$UV" sync --frozen 2>/dev/null || "$UV" sync)

# Render to a temp file and lint before installing so a bad render never lands in LaunchAgents.
TMP_PLIST="$(mktemp)"
sed -e "s|__UV__|$UV|g" -e "s|__REPO__|$REPO|g" -e "s|__LOGDIR__|$LOGDIR|g" \
  "$PLIST_SRC" > "$TMP_PLIST"
plutil -lint "$TMP_PLIST" >/dev/null
mv "$TMP_PLIST" "$PLIST_DST"

# Refuse to silently run invented devices on the real hub.
if grep -Eq '^HUB_FAKE_DEVICES=(1|true|True)' "$REPO/hub/.env"; then
  echo "WARNING: hub/.env sets HUB_FAKE_DEVICES. The hub will serve fake players, not your hardware."
fi

# Log rotation is done by the hub itself (HUB_LOG_FILE -> RotatingFileHandler, 10 MB x 7).
# launchd.out.log / launchd.err.log only receive pre-logging output and stay small.

# Restart cleanly whether or not it was already loaded.
launchctl bootout "gui/$UID_NUM/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$UID_NUM" "$PLIST_DST"
launchctl kickstart -k "gui/$UID_NUM/$LABEL"

sleep 2
PORT="$(grep -E '^HUB_PORT=' "$REPO/hub/.env" | cut -d= -f2 || true)"
PORT="${PORT:-8080}"
if curl -fsS "http://127.0.0.1:$PORT/api/health" >/dev/null 2>&1; then
  echo "illyHub running: http://$(hostname -s).local:$PORT/api/health"
else
  echo "Agent loaded but health check failed. Check $LOGDIR/hub.log and $LOGDIR/launchd.err.log"
  exit 1
fi
