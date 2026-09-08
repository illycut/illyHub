#!/usr/bin/env bash
# Self-update for the hub (PRD SET-2, ai-dev #72). Launched detached by POST /api/hub/update/apply
# (with ILLYHUB_STATUS_FILE / ILLYHUB_JOB_ID set), or run by hand: ./ops/update.sh
#
# Steps: refuse a dirty or detached checkout -> record HEAD -> git fetch + merge --ff-only ->
# uv sync -> build the PWA when node/npm are present -> write "succeeded"; the hub then exits on
# its own (see update.py) so launchd's KeepAlive relaunches it on the new code.
# Rollback: only when HEAD actually moved from the recorded commit. A refused merge (dirty tree,
# diverged branch, missing remote) leaves the checkout exactly as it was and writes "failed".
# If a step after the merge fails, git reset --hard to the recorded commit and re-sync; that is
# "rolled_back". If the rollback itself fails, "failed".
#
# Status protocol (GET /api/hub/update/status): $ILLYHUB_STATUS_FILE holds one line,
# "<state> <message>", state ∈ running | succeeded | up_to_date | failed | rolled_back. Writes are
# atomic (tmp + mv). Work runs as the checkout's owner (sudo -u) so the tree stays user-owned.
set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STATUS_FILE="${ILLYHUB_STATUS_FILE:-$REPO/hub/data/update/manual.status}"
JOB_ID="${ILLYHUB_JOB_ID:-manual}"
RESTART="${ILLYHUB_UPDATE_RESTART:-0}"     # 1 = kick launchd afterwards (manual runs only)
UPDATE_BRANCH="${HUB_UPDATE_BRANCH:-}"     # optional override of the branch to follow

mkdir -p "$(dirname "$STATUS_FILE")"
status() {
  local tmp="$STATUS_FILE.tmp.$$"
  printf '%s %s\n' "$1" "$2" > "$tmp" && mv -f "$tmp" "$STATUS_FILE"
  echo "[$JOB_ID] $1: $2"
}
status running "update started"

cd "$REPO" || { status failed "checkout not found at $REPO"; exit 2; }

# Run the git/uv/npm work as the checkout's owner so a root daemon never leaves root-owned files.
# BSD stat (macOS) takes -f %Su; GNU stat (Linux) treats -f as filesystem mode and prints block
# data instead of failing, so pick the form by platform rather than by fallback.
if [[ "$(uname -s)" == "Darwin" ]]; then
  OWNER="$(stat -f %Su "$REPO" 2>/dev/null || id -un)"
else
  OWNER="$(stat -c %U "$REPO" 2>/dev/null || id -un)"
fi
if [[ "$(id -un)" != "$OWNER" ]] && command -v sudo >/dev/null 2>&1; then
  AS_OWNER=(sudo -u "$OWNER" -H env "PATH=$PATH")
else
  AS_OWNER=(env "PATH=$PATH")
fi
run() { "${AS_OWNER[@]}" "$@"; }

UV="$(command -v uv 2>/dev/null || true)"
[[ -n "$UV" ]] || { status failed "uv not found on PATH ($PATH); reinstall with ops/install.sh"; exit 2; }

run git rev-parse --is-inside-work-tree >/dev/null 2>&1 || { status failed "not a git checkout"; exit 2; }
BRANCH="$(run git rev-parse --abbrev-ref HEAD 2>/dev/null || echo HEAD)"
if [[ "$BRANCH" == "HEAD" ]]; then
  status failed "the checkout is on a detached HEAD; check out a branch first"
  exit 2
fi
[[ -n "$UPDATE_BRANCH" ]] && BRANCH="$UPDATE_BRANCH"
if ! run git diff --quiet || ! run git diff --cached --quiet; then
  status failed "the checkout has uncommitted changes; commit or stash them before updating"
  exit 2
fi
PREV="$(run git rev-parse HEAD)"
echo "[$JOB_ID] at $PREV on $BRANCH (owner $OWNER)"

rollback() {
  local why="$1"
  local now
  now="$(run git rev-parse HEAD 2>/dev/null || echo "$PREV")"
  if [[ "$now" == "$PREV" ]]; then
    # Nothing moved: the checkout is untouched; say so and stop.
    status failed "$why"
    exit 1
  fi
  echo "[$JOB_ID] rolling back $now -> $PREV: $why"
  if run git reset --hard --quiet "$PREV" \
     && (cd hub && (run "$UV" sync --frozen 2>/dev/null || run "$UV" sync)); then
    status rolled_back "$why; back on $(run git rev-parse --short HEAD)"
  else
    status failed "$why; rollback to ${PREV:0:7} also failed"
  fi
  [[ "$RESTART" == "1" ]] && restart_hub
  exit 1
}

restart_hub() {
  if [[ "$(id -u)" -eq 0 ]] && launchctl print system/com.illyhub.hub >/dev/null 2>&1; then
    launchctl kickstart -k system/com.illyhub.hub || true
  fi
}

run git fetch --quiet origin || rollback "git fetch failed"
if ! run git merge --ff-only --quiet "origin/$BRANCH" 2>"$STATUS_FILE.err"; then
  rollback "git merge --ff-only origin/$BRANCH refused: $(tr '\n' ' ' < "$STATUS_FILE.err" | cut -c1-160)"
fi
rm -f "$STATUS_FILE.err"
NEW="$(run git rev-parse HEAD)"
if [[ "$NEW" == "$PREV" ]]; then
  status up_to_date "already up to date at $(run git rev-parse --short HEAD)"
  exit 0
fi
echo "[$JOB_ID] ${PREV:0:7} -> ${NEW:0:7}"

(cd hub && (run "$UV" sync --frozen 2>/dev/null || run "$UV" sync)) || rollback "uv sync failed"

if command -v npm >/dev/null 2>&1 && [[ -f app/package.json ]]; then
  if (cd app && run npm ci --no-audit --no-fund && run npm run build); then
    echo "[$JOB_ID] app built"
  else
    rollback "app build failed"
  fi
else
  echo "[$JOB_ID] npm not found on PATH; skipping the PWA build (hub serves the previous export)"
fi

status succeeded "updated ${PREV:0:7} -> ${NEW:0:7}"
[[ "$RESTART" == "1" ]] && restart_hub
exit 0
