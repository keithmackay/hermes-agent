"""Hermes no-agent cron job (05:00): report the nightly fork sync's result.

Reads the status.json that fork/sync/fork-sync.sh (on the Mac) writes into the
shared folder. Hermes delivers whatever this prints, and printing nothing makes
the tick silent. Stays silent when nothing changed, when the sync is still in
flight, or when the result was already reported. Mounted read-only at
$HERMES_HOME/scripts/fork/.
"""
import json
import os
import sys
import time
from datetime import datetime

HOME = os.environ.get("HERMES_HOME", "/opt/data")
SHARE = os.path.join(HOME, "fork-sync")
MARKER = os.path.join(HOME, "fork-sync-report.last")  # outside the share: writes there wake launchd
STUCK_TRIGGER_SECS = 30 * 60
STUCK_RUN_SECS = 2 * 60 * 60


def mtime(name):
    try:
        return os.path.getmtime(os.path.join(SHARE, name))
    except OSError:
        return None


def ago(ts):
    return datetime.fromtimestamp(ts).strftime("%a %H:%M")


def main():
    if not os.path.isdir(SHARE):
        print(f"fork-sync share {SHARE} is not mounted; see fork/docker-compose.mac.yml", file=sys.stderr)
        return 1
    now = time.time()

    pending = mtime("trigger")
    if pending is not None:
        if now - pending > STUCK_TRIGGER_SECS:
            print(f"Fork sync: the trigger from {ago(pending)} was never picked up by the Mac. Is the "
                  "launchd agent loaded? (fork/sync/install-launchd.sh) Was the Mac asleep or off?")
        return 0  # otherwise it was just fired (e.g. catch-up after wake): nothing to say yet

    running = mtime("processing")
    if running is not None:
        if now - running > STUCK_RUN_SECS:
            print(f"Fork sync: a run started {ago(running)} and still hasn't finished. "
                  "Check ~/Library/Logs/hermes-fork-sync.log on the Mac.")
        return 0

    try:
        with open(os.path.join(SHARE, "status.json")) as f:
            status = json.load(f)
    except FileNotFoundError:
        print("Fork sync: no run has completed yet.")
        return 0

    finished = datetime.strptime(status["finished_at"], "%Y-%m-%dT%H:%M:%S%z").timestamp()
    last_trigger = mtime("last_trigger")
    if last_trigger is not None and finished < last_trigger - 5:
        print(f"Fork sync: the trigger was taken at {ago(last_trigger)} but no result was recorded "
              "(the run may have crashed). Check ~/Library/Logs/hermes-fork-sync.log on the Mac.")
        return 0

    key = status["finished_at"]
    try:
        with open(MARKER) as f:
            if f.read().strip() == key:
                return 0
    except FileNotFoundError:
        pass
    with open(MARKER, "w") as f:
        f.write(key + "\n")

    result, step, message = status["result"], status.get("step"), status.get("message", "")
    warnings = status.get("warnings") or []
    if result == "noop" and not warnings:
        return 0
    line = {
        "ok": f"Fork sync OK: {message}",
        "noop": f"Fork sync: nothing to update. {message}",
        "skipped": f"Fork sync SKIPPED ({step}): {message}",
        "failed": f"Fork sync FAILED at {step}: {message}",
    }.get(result, f"Fork sync {result}: {message}")
    if warnings:
        line += " | Warnings: " + "; ".join(warnings)
    print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
