#!/bin/bash
# install-launchd.sh: install (or --uninstall) the launchd agent that runs
# fork-sync.sh when the Hermes cron trigger lands. Safe to re-run: it replaces
# any loaded copy. See docs/plans/2026-10-30-nightly-upstream-sync.md, Phase 3.
set -euo pipefail

LABEL=com.keithmackay.hermes-fork-sync
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
DEST="$HOME/Library/LaunchAgents/$LABEL.plist"
STATE_DIR="$HOME/.local/state/hermes-fork-sync"
DOMAIN="gui/$(id -u)"

launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true

if [ "${1:-}" = "--uninstall" ]; then
  rm -f "$DEST"
  echo "uninstalled $LABEL (state in $STATE_DIR left in place)"
  exit 0
fi

mkdir -p "$STATE_DIR" "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"
# Arm the 36 h staleness alarm from now, so a fresh install doesn't alarm.
[ -e "$STATE_DIR/last_trigger" ] || touch "$STATE_DIR/last_trigger"

sed -e "s|__REPO__|$REPO|g" -e "s|__HOME__|$HOME|g" "$HERE/$LABEL.plist" > "$DEST"
plutil -lint "$DEST" >/dev/null
launchctl bootstrap "$DOMAIN" "$DEST"
echo "installed $LABEL -> $DEST"
launchctl print "$DOMAIN/$LABEL" | grep -E '^\s*(state|program|path) =' || true
