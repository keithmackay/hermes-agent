#!/bin/bash
# fork-sync.sh: nightly upstream sync for keithmackay/hermes-agent (macOS host).
#
# Merges NousResearch/hermes-agent main into this fork, bumps the baked Claude
# Code CLI to the newest release that is at least 14 days old, builds, pushes,
# relaunches, and health-checks, rolling back to the previous image if the new
# one is unhealthy. Any failure leaves the running container untouched.
# Design and rationale: docs/plans/2026-10-30-nightly-upstream-sync.md
#
# Usage:
#   fork-sync.sh              run only if the Hermes cron trigger file exists (launchd mode)
#   fork-sync.sh --force-run  run now, ignoring the trigger
#   fork-sync.sh --dry-run    report what a run would do, using a throwaway worktree;
#                             never builds, pushes, or touches the working copy or status
#
# Test overrides (env): FORK_SYNC_REPO, FORK_SYNC_STATE_DIR, FORK_SYNC_LOG,
# FORK_SYNC_UPSTREAM_URL, FORK_SYNC_MIN_AGE_DAYS, FORK_SYNC_HEALTH_TIMEOUT, FORK_SYNC_NOTIFY=0,
# FORK_SYNC_UNSAFE_ANY_CHECKOUT=1 (skips the ownership guard; tests only, and only
# with a compose file that cannot build: images and containers are global).
#
# Writes $STATE_DIR/status.json (read by the Hermes fork-sync-report cron job)
# and logs to ~/Library/Logs/hermes-fork-sync.log.
#
# Compatible with macOS /bin/bash 3.2. The whole body sits inside main(), so
# bash parses it fully before running: the merge can rewrite this very file
# mid-run.

set -u -o pipefail

main() {
  local mode=trigger
  case "${1:-}" in
    "") ;;
    --force-run) mode=force ;;
    --dry-run) mode=dry ;;
    -h|--help) sed -n '2,20p' "$0"; return 0 ;;
    *) echo "unknown argument: $1 (try --help)" >&2; return 2 ;;
  esac

  export PATH="/opt/homebrew/bin:/usr/local/bin:/Applications/Docker.app/Contents/Resources/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"

  REPO="${FORK_SYNC_REPO:-$(cd "$(dirname "$0")/../.." && pwd)}"
  STATE_DIR="${FORK_SYNC_STATE_DIR:-$HOME/.local/state/hermes-fork-sync}"
  LOG="${FORK_SYNC_LOG:-$HOME/Library/Logs/hermes-fork-sync.log}"
  HEALTH_TIMEOUT="${FORK_SYNC_HEALTH_TIMEOUT:-180}"
  HEALTH_URL="${FORK_SYNC_HEALTH_URL:-http://127.0.0.1:9119/api/status}"
  UPSTREAM_URL="${FORK_SYNC_UPSTREAM_URL:-https://github.com/NousResearch/hermes-agent.git}"
  BRANCH=main
  MIN_AGE_DAYS="${FORK_SYNC_MIN_AGE_DAYS:-14}"
  IMAGE=hermes-agent:mac-local
  PREV_IMAGE=hermes-agent:mac-previous
  COMPOSE_FILE="$REPO/fork/docker-compose.mac.yml"

  mkdir -p "$STATE_DIR" "$(dirname "$LOG")"

  # Trigger mode exits silently (no log, no status) when there is nothing to do,
  # so the 15-minute launchd fallback stays quiet, except for the staleness alarm.
  if [ "$mode" = trigger ] && [ ! -e "$STATE_DIR/trigger" ]; then
    check_trigger_staleness
    return 0
  fi

  setup_logging
  acquire_lock || return 0

  if [ "$mode" = trigger ]; then
    mv "$STATE_DIR/trigger" "$STATE_DIR/processing" 2>/dev/null || { log "trigger already taken"; return 0; }
    touch "$STATE_DIR/last_trigger"; rm -f "$STATE_DIR/stale_notified"
  fi

  if [ "$mode" = dry ]; then
    dry_run
    return $?
  fi

  ACTIVE=1   # from here on, on_exit records a status
  STARTED_AT="$(now_iso)"
  log "=== fork sync start ($mode) ==="

  preflight || return 1
  pull_fork || return 1
  merge_upstream || return 1
  bump_claude_code || return 1

  END_SHA="$(git rev-parse HEAD)"
  local origin_sha deployed_sha
  origin_sha="$(git rev-parse "origin/$BRANCH")"
  deployed_sha="$(cat "$STATE_DIR/deployed_sha" 2>/dev/null || true)"
  if [ "$END_SHA" = "$START_SHA" ] && [ "$origin_sha" = "$END_SHA" ] && [ "$deployed_sha" = "$END_SHA" ]; then
    finish noop "" "Already up to date with upstream; Claude Code $CC_FROM"
    return 0
  fi

  write_install_stamp
  build_images || return 1
  push_fork || return 1
  deploy || return 1
  tidy_images

  printf '%s\n' "$END_SHA" > "$STATE_DIR/deployed_sha"
  local summary="Deployed ${END_SHA:0:10}"
  [ "$UPSTREAM_COMMITS" -gt 0 ] && summary="$summary; merged $UPSTREAM_COMMITS upstream commits"
  [ "$CC_TO" != "$CC_FROM" ] && summary="$summary; Claude Code $CC_FROM -> $CC_TO"
  finish ok "" "$summary"
}

# ---------------------------------------------------------------- plumbing

ACTIVE=0; RESULT=""; STEP=""; MESSAGE=""; WARNINGS=""
STARTED_AT=""; START_SHA=""; END_SHA=""; UPSTREAM_COMMITS=0; CC_FROM=""; CC_TO=""
LOCK_HELD=0; STAMP_WRITTEN=0; WORKTREE=""

now_iso() { date '+%Y-%m-%dT%H:%M:%S%z'; }

log() { printf '%s %s\n' "$(date '+%F %T')" "$*"; }

warn() { log "WARNING: $*"; WARNINGS="${WARNINGS}${WARNINGS:+$'\n'}$*"; }

setup_logging() {
  # Rotate at 5 MB (nightly builds are chatty), then send all output to the log;
  # interactive runs also see it on the terminal.
  if [ -f "$LOG" ] && [ "$(stat -f %z "$LOG")" -gt 5242880 ]; then mv -f "$LOG" "$LOG.1"; fi
  if [ -t 1 ]; then exec > >(tee -a "$LOG") 2>&1; else exec >>"$LOG" 2>&1; fi
}

acquire_lock() {
  local lock="$STATE_DIR/lock"
  if mkdir "$lock" 2>/dev/null; then
    echo $$ > "$lock/pid"; LOCK_HELD=1; return 0
  fi
  local pid; pid="$(cat "$lock/pid" 2>/dev/null || true)"
  if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
    log "another fork-sync run (pid $pid) holds the lock; exiting"
    return 1
  fi
  log "removing stale lock (pid ${pid:-unknown})"
  rm -rf "$lock" && mkdir "$lock" && echo $$ > "$lock/pid" && LOCK_HELD=1
}

# If Hermes hasn't dropped a trigger in 36 h (container down, cron job paused or
# removed), say so, at most once a day. Armed by install-launchd.sh, which
# creates last_trigger; until then there is nothing to compare against.
check_trigger_staleness() {
  local last="$STATE_DIR/last_trigger" mark="$STATE_DIR/stale_notified"
  [ -e "$last" ] || return 0
  [ -n "$(find "$last" -mmin +2160 2>/dev/null)" ] || return 0
  if [ -e "$mark" ] && [ -z "$(find "$mark" -mmin +1440 2>/dev/null)" ]; then return 0; fi
  touch "$mark"
  printf '%s %s\n' "$(date '+%F %T')" "WARNING: no trigger from Hermes in 36h+" >> "$LOG"
  notify "No nightly sync trigger from Hermes in 36h+. Is the hermes container running and the fork-sync-trigger cron job enabled?"
}

# fail STEP MESSAGE: record a failure; the caller returns 1.
fail() { finish failed "$1" "$2"; }

# skip STEP MESSAGE: record a deliberate skip (nothing touched).
skip() { finish skipped "$1" "$2"; }

finish() {
  RESULT="$1"; STEP="$2"; MESSAGE="$3"
  log "result=$RESULT${STEP:+ step=$STEP}: $MESSAGE"
}

on_exit() {
  local code=$?
  if [ "$STAMP_WRITTEN" = 1 ]; then rm -f "$REPO/install-stamp.json"; fi
  if [ -n "$WORKTREE" ]; then
    git -C "$REPO" worktree remove --force "$WORKTREE" >/dev/null 2>&1 || true
    rm -rf "$(dirname "$WORKTREE")"
  fi
  if [ "$ACTIVE" = 1 ]; then
    if [ -z "$RESULT" ]; then finish failed "${STEP:-unknown}" "unexpected exit (code $code)"; fi
    write_status
    case "$RESULT" in
      failed|skipped) notify "$RESULT${STEP:+ at $STEP}: $MESSAGE" ;;
    esac
    rm -f "$STATE_DIR/processing"
    log "=== fork sync end ($RESULT) ==="
  fi
  if [ "$LOCK_HELD" = 1 ]; then rm -rf "$STATE_DIR/lock"; fi
}
trap on_exit EXIT

write_status() {
  FS_STARTED_AT="$STARTED_AT" FS_FINISHED_AT="$(now_iso)" FS_RESULT="$RESULT" FS_STEP="$STEP" \
  FS_MESSAGE="$MESSAGE" FS_WARNINGS="$WARNINGS" FS_UPSTREAM_COMMITS="$UPSTREAM_COMMITS" \
  FS_CC_FROM="$CC_FROM" FS_CC_TO="$CC_TO" FS_START_SHA="$START_SHA" FS_END_SHA="$END_SHA" \
  python3 - "$STATE_DIR/status.json" <<'PY'
import json, os, sys, tempfile
e = os.environ
status = {
    "started_at": e["FS_STARTED_AT"], "finished_at": e["FS_FINISHED_AT"],
    "result": e["FS_RESULT"], "step": e["FS_STEP"] or None, "message": e["FS_MESSAGE"],
    "warnings": [w for w in e["FS_WARNINGS"].split("\n") if w],
    "upstream_commits": int(e["FS_UPSTREAM_COMMITS"] or 0),
    "claude_code_from": e["FS_CC_FROM"] or None, "claude_code_to": e["FS_CC_TO"] or None,
    "start_sha": e["FS_START_SHA"] or None, "end_sha": e["FS_END_SHA"] or None,
}
path = sys.argv[1]
fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".status.")
with os.fdopen(fd, "w") as f:
    json.dump(status, f, indent=2)
    f.write("\n")
os.chmod(tmp, 0o644)
os.replace(tmp, path)
PY
}

notify() {
  [ "${FORK_SYNC_NOTIFY:-1}" = 1 ] || return 0
  local msg; msg="$(printf '%s' "$1" | tr '"\\' "''")"
  osascript -e "display notification \"${msg:0:240}\" with title \"Hermes fork sync\"" >/dev/null 2>&1 || true
}

# ------------------------------------------------------------------- steps

preflight() {
  STEP=preflight
  cd "$REPO" || { fail preflight "repo not found at $REPO"; return 1; }
  local branch; branch="$(git symbolic-ref --short -q HEAD || echo DETACHED)"
  if [ "$branch" != "$BRANCH" ]; then
    skip preflight "repo is on '$branch', not $BRANCH; not touching it"; return 1
  fi
  if [ -e "$(git rev-parse --git-path MERGE_HEAD)" ] || [ -d "$(git rev-parse --git-path rebase-merge)" ] || [ -d "$(git rev-parse --git-path rebase-apply)" ]; then
    skip preflight "a merge or rebase is in progress; not touching it"; return 1
  fi
  if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
    skip preflight "uncommitted changes in $REPO; not touching them"; return 1
  fi
  docker info >/dev/null 2>&1 || { fail preflight "Docker is not running"; return 1; }
  # Image and container names are global to Docker, not per checkout: only the
  # checkout whose compose file created the running container may rebuild it
  # (a stray clone once redeployed the live container this way). A container
  # created from a compose file that no longer exists is adopted.
  local owner
  owner="$(docker inspect -f '{{index .Config.Labels "com.docker.compose.project.config_files"}}' hermes 2>/dev/null || true)"
  if [ -n "$owner" ] && [ "$owner" != "$COMPOSE_FILE" ] && [ -e "$owner" ] && [ "${FORK_SYNC_UNSAFE_ANY_CHECKOUT:-0}" != 1 ]; then
    fail preflight "the running container belongs to $owner, not this checkout ($COMPOSE_FILE)"; return 1
  fi
  # Compare the stored URL: `git remote get-url` applies url.*.insteadOf rewrites
  # (e.g. a global https -> ssh rule), which would never match.
  local url; url="$(git config --get remote.upstream.url || true)"
  if [ -z "$url" ]; then
    git remote add upstream "$UPSTREAM_URL" || { fail preflight "could not add upstream remote"; return 1; }
  elif [ "$url" != "$UPSTREAM_URL" ]; then
    fail preflight "upstream remote is $url, expected $UPSTREAM_URL"; return 1
  fi
  START_SHA="$(git rev-parse HEAD)"
  CC_FROM="$(current_cc_version)"; CC_TO="$CC_FROM"
  log "preflight ok: $BRANCH at ${START_SHA:0:10}, Claude Code $CC_FROM"
}

pull_fork() {
  STEP=pull
  git fetch --quiet origin || { fail pull "git fetch origin failed"; return 1; }
  git merge --ff-only --quiet "origin/$BRANCH" || {
    fail pull "local $BRANCH and origin/$BRANCH have diverged; resolve by hand"; return 1; }
  log "fork pulled: $BRANCH at $(git rev-parse --short=10 HEAD)"
}

merge_upstream() {
  STEP=merge
  git fetch --quiet upstream || { fail merge "git fetch upstream failed"; return 1; }
  UPSTREAM_COMMITS="$(git rev-list --count "HEAD..upstream/$BRANCH")"
  if [ "$UPSTREAM_COMMITS" -eq 0 ]; then log "no new upstream commits"; return 0; fi
  log "merging $UPSTREAM_COMMITS upstream commits"
  if ! git merge --no-edit --quiet -m "Merge upstream NousResearch/hermes-agent $BRANCH ($UPSTREAM_COMMITS commits)" "upstream/$BRANCH"; then
    local conflicts; conflicts="$(git diff --name-only --diff-filter=U | head -15 | tr '\n' ' ')"
    git merge --abort
    fail merge "conflicts with upstream in: ${conflicts:-unknown files}"; return 1
  fi
}

current_cc_version() {
  sed -n 's/^ARG CLAUDE_CODE_VERSION=//p' "${1:-.}/fork/Dockerfile" 2>/dev/null | head -1
}

# Newest X.Y.Z where both the main package and this host's linux platform
# package were published at least MIN_AGE_DAYS ago (.npmrc min-release-age
# applies to the platform-native optional dependency too).
pick_cc_version() {
  local arch plat tmp
  arch="$(docker info --format '{{.Architecture}}' 2>/dev/null || uname -m)"
  case "$arch" in aarch64|arm64) plat=linux-arm64 ;; *) plat=linux-x64 ;; esac
  tmp="$(mktemp -d)"
  ( cd / && npm view @anthropic-ai/claude-code time --json >"$tmp/main.json" 2>/dev/null &&
    npm view "@anthropic-ai/claude-code-$plat" time --json >"$tmp/plat.json" 2>/dev/null ) || { rm -rf "$tmp"; return 1; }
  python3 - "$tmp/main.json" "$tmp/plat.json" "$MIN_AGE_DAYS" <<'PY'
import json, re, sys
from datetime import datetime, timedelta, timezone
main, plat = (json.load(open(p)) for p in sys.argv[1:3])
cutoff = datetime.now(timezone.utc) - timedelta(days=int(sys.argv[3]))
def old_enough(times, v):
    t = times.get(v)
    return bool(t) and datetime.fromisoformat(t.replace("Z", "+00:00")) <= cutoff
ok = [v for v in main if re.fullmatch(r"\d+\.\d+\.\d+", v) and old_enough(main, v) and old_enough(plat, v)]
if not ok:
    sys.exit(1)
print(max(ok, key=lambda v: tuple(map(int, v.split(".")))))
PY
  local rc=$?; rm -rf "$tmp"; return $rc
}

version_gt() {
  python3 -c 'import sys; a, b = (tuple(map(int, v.split("."))) for v in sys.argv[1:3]); sys.exit(0 if a > b else 1)' "$1" "$2"
}

bump_claude_code() {
  STEP=bump
  local cand
  if ! cand="$(pick_cc_version)" || [ -z "$cand" ]; then
    warn "could not determine a Claude Code release >= $MIN_AGE_DAYS days old; keeping $CC_FROM"; return 0
  fi
  if [ -n "$CC_FROM" ] && ! version_gt "$cand" "$CC_FROM"; then
    log "Claude Code $CC_FROM is current (newest eligible: $cand)"; return 0
  fi
  sed -i '' "s/^ARG CLAUDE_CODE_VERSION=.*/ARG CLAUDE_CODE_VERSION=$cand/" fork/Dockerfile
  git commit --quiet -m "chore(fork): bump Claude Code to $cand" -- fork/Dockerfile ||
    { fail bump "could not commit Claude Code bump"; return 1; }
  CC_TO="$cand"
  log "Claude Code $CC_FROM -> $CC_TO"
}

# Version stamp the way upstream CI does it (git-ignored file, picked up by the
# root Dockerfile's COPY . .). Removed again on exit so it never goes stale.
write_install_stamp() {
  STEP=stamp
  local display base dev distance
  display="$(python3 -m scripts.releases.distance 2>/dev/null)" || { warn "version stamp skipped (distance helper failed)"; return 0; }
  base="${display%%+*}"; dev="${display#*+}"
  if [ "$dev" = "$display" ]; then distance=0; else distance="${dev%%.*}"; fi
  if python3 scripts/write_install_stamp.py --output install-stamp.json --commit "$(git rev-parse HEAD)" \
      --base-version "$base" --display-version "$display" --distance "$distance" \
      --source local --distribution docker --update-mechanism external >/dev/null; then
    STAMP_WRITTEN=1; log "version stamp: $display"
  else
    warn "version stamp skipped (write_install_stamp.py failed)"
  fi
}

PREV_ID=""; NEW_ID=""

image_id() { docker image inspect -f '{{.Id}}' "$1" 2>/dev/null || true; }

build_images() {
  STEP=build
  # "Previous" = what the container is actually running, which can differ from
  # the mac-local tag (e.g. after a run that built but failed to push).
  PREV_ID="$(docker inspect -f '{{.Image}}' "$(docker compose -f "$COMPOSE_FILE" ps -q hermes 2>/dev/null)" 2>/dev/null || true)"
  [ -n "$PREV_ID" ] || PREV_ID="$(image_id "$IMAGE")"
  # Tag it BEFORE building. Docker Desktop's containerd image store deletes an
  # image the moment its last tag moves to a new build, even while a container
  # still runs from it, so tagging afterwards finds nothing to tag.
  if [ -n "$PREV_ID" ]; then
    docker tag "$PREV_ID" "$PREV_IMAGE" || { fail build "could not tag the running image as $PREV_IMAGE"; return 1; }
  fi
  log "building images"
  if ! docker compose -f "$COMPOSE_FILE" build; then
    if [ "$(git rev-parse HEAD)" != "$START_SHA" ]; then
      git reset --quiet --hard "$START_SHA"   # preflight guaranteed a clean tree
      log "local $BRANCH reset to ${START_SHA:0:10}"
    fi
    fail build "docker build failed; nothing pushed, old container still running"; return 1
  fi
  NEW_ID="$(image_id "$IMAGE")"
}

push_fork() {
  STEP=push
  if [ "$(git rev-parse HEAD)" = "$(git rev-parse "origin/$BRANCH")" ]; then return 0; fi
  git push --quiet origin "HEAD:$BRANCH" ||
    { fail push "git push to origin failed; built but not deployed (next run retries)"; return 1; }
  log "pushed $BRANCH to origin"
}

healthy() {
  local deadline=$((SECONDS + $1)) cid running_id
  while [ "$SECONDS" -lt "$deadline" ]; do
    cid="$(docker compose -f "$COMPOSE_FILE" ps -q hermes 2>/dev/null)"
    running_id="$(docker inspect -f '{{.Image}}' "$cid" 2>/dev/null || true)"
    if [ "$running_id" = "$2" ] &&
       curl -fsS --max-time 5 "$HEALTH_URL" 2>/dev/null |
         python3 -c 'import json, sys; sys.exit(0 if json.load(sys.stdin).get("gateway_running") is True else 1)' 2>/dev/null; then
      return 0
    fi
    sleep 5
  done
  return 1
}

deploy() {
  STEP=deploy
  docker compose -f "$COMPOSE_FILE" up -d --no-build ||
    { fail deploy "docker compose up failed"; return 1; }
  STEP=health
  if healthy "$HEALTH_TIMEOUT" "$NEW_ID"; then log "new container healthy"; return 0; fi
  if [ -z "$PREV_ID" ] || [ "$PREV_ID" = "$NEW_ID" ]; then
    fail health "new container unhealthy after ${HEALTH_TIMEOUT}s and no previous image to roll back to"; return 1
  fi
  log "new container unhealthy; rolling back to previous image (failed image kept as hermes-agent:mac-failed)"
  docker tag "$NEW_ID" hermes-agent:mac-failed
  docker tag "$PREV_ID" "$IMAGE"
  docker compose -f "$COMPOSE_FILE" up -d --no-build
  if healthy 120 "$PREV_ID"; then
    fail health "new image unhealthy; rolled back to the previous image (code is pushed; next run retries)"
  else
    fail health "new image unhealthy AND rollback unhealthy; check 'docker logs hermes'"
  fi
  return 1
}

# Superseded images disappear with their last tag (containerd store), so only
# build cache needs trimming: drop entries unused for a week.
tidy_images() {
  docker builder prune -f --filter until=168h >/dev/null 2>&1 || true
}

# ----------------------------------------------------------------- dry run

dry_run() {
  log "=== fork sync DRY RUN ==="
  cd "$REPO" || { echo "repo not found at $REPO"; return 1; }
  local branch; branch="$(git symbolic-ref --short -q HEAD || echo DETACHED)"
  [ "$branch" = "$BRANCH" ] || log "would SKIP: repo is on '$branch', not $BRANCH"
  [ -z "$(git status --porcelain --untracked-files=no)" ] || log "would SKIP: uncommitted changes"
  docker info >/dev/null 2>&1 || log "would FAIL: Docker is not running"
  git remote get-url upstream >/dev/null 2>&1 || git remote add upstream "$UPSTREAM_URL"

  git fetch --quiet origin && git fetch --quiet upstream || { log "fetch failed"; return 1; }
  log "local vs origin/$BRANCH: $(git rev-list --count "origin/$BRANCH..HEAD") ahead, $(git rev-list --count "HEAD..origin/$BRANCH") behind"
  if ! git merge-base --is-ancestor HEAD "origin/$BRANCH" && ! git merge-base --is-ancestor "origin/$BRANCH" HEAD; then
    log "would FAIL: local and origin/$BRANCH have diverged"
  fi
  local n; n="$(git rev-list --count "origin/$BRANCH..upstream/$BRANCH")"
  log "upstream commits to merge: $n"

  WORKTREE="$(mktemp -d)/wt"
  git worktree add --quiet --detach "$WORKTREE" "origin/$BRANCH" || { log "could not create worktree"; return 1; }
  if [ "$n" -gt 0 ]; then
    if git -C "$WORKTREE" merge --no-commit --no-ff --quiet "upstream/$BRANCH" >/dev/null 2>&1; then
      log "merge: clean"
    else
      log "merge: would CONFLICT in: $(git -C "$WORKTREE" diff --name-only --diff-filter=U | head -15 | tr '\n' ' ')"
    fi
    git -C "$WORKTREE" merge --abort >/dev/null 2>&1 || true
  fi

  local cur cand; cur="$(current_cc_version "$WORKTREE")"
  if cand="$(pick_cc_version)"; then
    if version_gt "$cand" "$cur"; then log "Claude Code: would bump $cur -> $cand"; else log "Claude Code: $cur is current (newest eligible: $cand)"; fi
  else
    log "Claude Code: could not determine an eligible release (would keep $cur)"
  fi
  log "deployed: $(cat "$STATE_DIR/deployed_sha" 2>/dev/null | cut -c1-10 || true) (none recorded = first real run rebuilds)"
  log "=== dry run done: nothing built, pushed, or changed ==="
}

main "$@"; exit $?
