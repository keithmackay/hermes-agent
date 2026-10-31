#!/bin/bash
# Hermes no-agent cron job (04:00): ring the doorbell for the nightly fork sync.
#
# Creates `trigger` in the folder shared with the Mac; the launchd agent
# watching it runs fork/sync/fork-sync.sh. The host only checks that the file
# exists and never reads it, so there's no need to write it atomically.
# Prints nothing (a silent tick). A non-zero exit makes Hermes deliver an
# error alert. Mounted read-only at $HERMES_HOME/scripts/fork/.
set -eu
SHARE="${HERMES_HOME:-/opt/data}/fork-sync"
if [ ! -d "$SHARE" ]; then
  echo "fork-sync share $SHARE is not mounted; see fork/docker-compose.mac.yml" >&2
  exit 1
fi
date -u +%Y-%m-%dT%H:%M:%SZ > "$SHARE/trigger"
