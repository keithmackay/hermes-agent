# Plan: nightly upstream sync and rebuild

**Status:** proposed, not yet implemented
**Date:** 2026-10-30
**Change log:** record each phase in [`docs/specs/fork-changes.md`](../specs/fork-changes.md) as it lands.

## Goal

Every night at 04:00 (America/New_York), bring this fork (`keithmackay/hermes-agent`) up to date with
upstream (`NousResearch/hermes-agent`), keep our additions on top, move the baked Claude Code CLI to
the newest release that is at least 14 days old, and rebuild and relaunch the Docker Desktop container.
A bad night must never leave us worse off: if any step fails, the running container stays as it is
and we get an alert.

## Design decisions

### 1. Hermes owns the schedule; the Mac does the privileged work (hybrid)

Hermes runs inside the container it would be rebuilding, and it has no Docker or GitHub-push access
(by design: see the security notes in `fork/docker-compose.mac.yml`). So the work is split:

| Piece | Runs in | Job |
|---|---|---|
| `fork-sync-trigger` Hermes cron job, 04:00 | container, `--no-agent` | Drops a trigger file into a shared folder. Silent. |
| `fork-sync` launchd agent | macOS host | Sees the trigger, runs `fork/sync/fork-sync.sh` with the Mac's own git (SSH) and Docker. Writes a status file. |
| `fork-sync-report` Hermes cron job, 05:00 | container, `--no-agent` | Reads the status file and reports. Silent when nothing changed. |

Why not `/heartbeat`: Hermes heartbeats fire on an interval ("every 1d") inside one chat session and
can't be pinned to a wall-clock time. `hermes cron` can, and it survives restarts. Both cron jobs use
**no-agent mode** (a script on a schedule, no LLM, no tokens). Empty stdout means a silent tick, and a
non-zero exit sends an error alert.

Why not do everything in the container: that would mean mounting the Docker socket, which is
root-equivalent over Docker Desktop and reachable from the agent's terminal tool, plus putting a GitHub
push token in the container. It would also mean handing the rebuild off to a sibling container, because
`docker compose up` would stop the container the script runs in.

### 2. Merge locally and push; never use `gh repo sync`

`gh repo sync` only fast-forwards. Our fork has its own commits, so it would fail every night, and its
`--force` flag hard-resets the fork to upstream, **erasing our commits**. Instead the host script merges
`upstream/main` into local `main` and pushes the result to the fork. The end state matches the intended
order (fork updated from upstream, local matches fork), and a conflict is caught before anything is pushed.

### 3. Keep everything that is ours in `fork/`, and leave upstream files untouched

The nightly merge stays conflict-free only if we don't edit upstream files. Today we do: the Claude Code
install is spliced into the root `Dockerfile`, and `.gitignore` has one extra line. Phase 1 moves all
fork-specific material into `fork/`, which upstream will never touch:

```
fork/
  Dockerfile               # FROM the upstream-built image; adds Claude Code (CLAUDE_CODE_VERSION lives here)
  docker-compose.mac.yml   # moved from the repo root
  .env.mac.example         # moved from the repo root
  .gitignore               # ignores .env.mac
  hermes-scripts/          # the no-agent cron scripts, mounted read-only into the container
    fork-sync-trigger.sh
    fork-sync-report.sh
  sync/
    fork-sync.sh           # the host-side worker
    com.keithmackay.hermes-fork-sync.plist
```

`docs/plans/` and `docs/specs/` stay at the repo root, as requested. Upstream has no root `docs/` folder.

### 4. Build before pushing, and keep the previous image for rollback

`docker compose up -d --build` builds and then swaps containers. The script splits that into
`build`, then `push`, then `up -d`, so that:

- a failed build never reaches the fork: local `main` is reset to the commit it started from, and the old container keeps running
- the image currently running is tagged `hermes-agent:mac-previous` before the build, so an unhealthy new container can be swapped back

## Phase 1: move fork-only files into `fork/` (one-time)

1. `git mv docker-compose.mac.yml .env.mac.example fork/`, and move `.env.mac` (untracked) to `fork/.env.mac`.
2. Revert the Claude Code block in the root `Dockerfile` and the `.env.mac` line in the root `.gitignore`
   to upstream (`git checkout upstream/main -- Dockerfile .gitignore`). Add `fork/.gitignore` containing `.env.mac`.
3. Create `fork/Dockerfile` as a thin layer on the upstream image:
   ```dockerfile
   FROM hermes-base
   USER root
   ARG CLAUDE_CODE_VERSION=2.1.296
   RUN npm install -g --prefix /usr/local --no-audit --fetch-retries=5 \
           --allow-scripts=@anthropic-ai/claude-code \
           "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" && \
       npm cache clean --force && claude --version
   ENV DISABLE_AUTOUPDATER=1
   ```
   Keep the existing comment block explaining the two npm guards (14-day `min-release-age`, which also
   applies to the platform-native package, and the install-script allowlist). Check which `USER` the
   upstream image ends on and restore it after the install if it isn't root. The entrypoint stays
   inherited.
4. In `fork/docker-compose.mac.yml`, build the upstream image as a named context and layer on it:
   ```yaml
   services:
     hermes:
       build:
         context: .
         dockerfile: Dockerfile
         additional_contexts:
           hermes-base: "service:hermes-base"
       image: hermes-agent:mac-local
       # ...existing settings...
       environment:
         - HERMES_TIMEZONE=America/New_York   # cron is UTC otherwise; container has no TZ
       volumes:
         - hermes-data:/opt/data
         - ~/.local/state/hermes-fork-sync:/opt/data/fork-sync
         - ./hermes-scripts:/opt/data/scripts/fork:ro
     hermes-base:
       build:
         context: ..
       image: hermes-agent:upstream-local
       profiles: ["build-only"]   # never started, only built
   ```
   **Verify** that `compose build` builds a `service:` context whose service is in an inactive profile.
   If it doesn't, drop the profile and give `hermes-base` `command: ["true"]` with `restart: "no"`. As a
   last resort, have `fork-sync.sh` run `docker build -t hermes-agent:upstream-local ..` first and use
   `FROM hermes-agent:upstream-local`.
5. Rebuild and relaunch. Confirm that `claude --version` works as the `hermes` user, that the dashboard
   still redirects to `/login`, that the API returns 401 without the key and 200 with it, and that
   `docker exec hermes date` and `hermes cron list` show New York times.
6. Commit as "Move fork-specific files into fork/; layer Claude Code on the upstream image" and log it
   in `docs/specs/fork-changes.md`.

## Phase 2: host worker `fork/sync/fork-sync.sh`

Wrap the whole body in `main() { ... }; main "$@"`. Bash reads scripts as it runs them, and the merge
can rewrite this very file mid-run. Support `--dry-run` and `--force-run` (ignore the missing trigger).

Steps, stopping at the first failure:

1. **Gate.** Exit quietly unless `~/.local/state/hermes-fork-sync/trigger` exists, or `--force-run` was
   passed. Move the trigger to `processing` so a second launchd fire is a no-op. Use the trigger only as
   a doorbell and never read its contents: the agent can write to that folder, so its contents are
   untrusted.
2. **Lock.** `mkdir ~/.local/state/hermes-fork-sync/lock`, or exit if it already exists. Remove it with a trap on exit.
3. **Preflight.** Check that the repo is on `main` with a clean working tree (otherwise alert "skipped:
   uncommitted changes" and stop, so in-progress work is never touched), that Docker is reachable, and
   that the `upstream` remote exists (add `https://github.com/NousResearch/hermes-agent.git` if not).
   Record `START_SHA=$(git rev-parse HEAD)`.
4. **Pull the fork.** `git pull --ff-only origin main`.
5. **Merge upstream.** `git fetch upstream && git merge --no-edit upstream/main`. On a conflict, run
   `git merge --abort` and alert with the conflicting paths. Nothing has been pushed or built at this point.
6. **Bump Claude Code.** Find the newest non-prerelease version where both `@anthropic-ai/claude-code`
   and `@anthropic-ai/claude-code-linux-arm64` were published at least 14 days ago (`npm view <pkg> time
   --json`). If it differs from `ARG CLAUDE_CODE_VERSION` in `fork/Dockerfile`, rewrite that line and
   commit "chore(fork): bump Claude Code to X".
7. **Build.** `docker tag hermes-agent:mac-local hermes-agent:mac-previous`, then
   `docker compose -f fork/docker-compose.mac.yml build`. On failure, `git reset --hard $START_SHA`
   (the preflight guaranteed a clean tree) and alert.
8. **Push.** `git push origin main` over the Mac's SSH key.
9. **Swap.** `docker compose -f fork/docker-compose.mac.yml up -d`.
10. **Health check.** For up to 180 s, poll `http://127.0.0.1:9119/api/status` for `"gateway_running":true`.
    If it never appears, `docker tag hermes-agent:mac-previous hermes-agent:mac-local`, run `up -d` again,
    and alert "running previous image". The code is already pushed, so tomorrow's run retries.
11. **Tidy.** `docker image prune -f` and `docker builder prune -f --keep-storage 20GB`. Nightly builds
    pile up otherwise.
12. **Status.** Atomically write `~/.local/state/hermes-fork-sync/status.json` with `{finished_at, result:
    ok|noop|skipped|failed, step, upstream_commits, claude_code_from, claude_code_to, message}`. Log to
    `~/Library/Logs/hermes-fork-sync.log`. On failure, also show a macOS notification
    (`osascript -e 'display notification ...'`) so a failure is visible even if Hermes is down.

`--dry-run` runs steps 1 to 6 against a throwaway `git worktree` (merging with `--no-commit`), prints
what it would do, and never builds, pushes, or touches the working copy.

## Phase 3: launchd agent

`fork/sync/com.keithmackay.hermes-fork-sync.plist`, installed to `~/Library/LaunchAgents/` and loaded
with `launchctl bootstrap gui/$(id -u) <plist>`:

- `WatchPaths`: `~/.local/state/hermes-fork-sync` (absolute path). Fires when the trigger lands.
- `StartInterval`: `900` as a fallback. The script exits quietly when there's no trigger.
- `ProgramArguments`: `/bin/bash -lc "<repo>/fork/sync/fork-sync.sh"`, so the login shell finds Docker, npm, and git.
- `StandardOutPath`/`StandardErrorPath`: `~/Library/Logs/hermes-fork-sync.log`.

**Verify** that a file written from inside the container through the bind mount actually triggers
`WatchPaths`. If it doesn't, the 15-minute `StartInterval` still catches it.

Optional safety net: on the `StartInterval` path, if no trigger has arrived in 36 h, show a macOS
notification ("Hermes nightly sync hasn't been triggered: is the container running?").

## Phase 4: Hermes cron jobs

Scripts live in `fork/hermes-scripts/` and are mounted read-only at `/opt/data/scripts/fork/`. Hermes
only runs scripts inside `$HERMES_HOME/scripts/`, and the read-only mount means the agent can't edit them.

- `fork-sync-trigger.sh`: atomically write `/opt/data/fork-sync/trigger` (write a temp file, then `mv`)
  and print nothing.
- `fork-sync-report.sh`: read `/opt/data/fork-sync/status.json`. Print a one-line summary when something
  changed ("Synced 37 upstream commits; Claude Code 2.1.296 → 2.1.31x"), the failure step and message on
  failure, and "no sync ran since the 04:00 trigger" when the status is older than the trigger. Print
  nothing on a no-op.

```bash
docker exec hermes hermes cron create "0 4 * * *" --no-agent \
  --script fork/fork-sync-trigger.sh --name fork-sync-trigger
docker exec hermes hermes cron create "0 5 * * *" --no-agent \
  --script fork/fork-sync-report.sh --name fork-sync-report --deliver local
```

Delivery starts as `local` (`~/.hermes/cron/output/`). Switch the report job to a messaging target with
`hermes cron edit <id> --deliver <platform>` once one is set up in Hermes.

If the Mac is asleep at 04:00, Hermes fires the trigger once on wake (`cron.catch_up_missed` defaults to true).

## Phase 5: test before trusting it

1. `fork/sync/fork-sync.sh --dry-run` prints the upstream commit count, any merge conflicts, and the chosen Claude Code version.
2. `fork/sync/fork-sync.sh --force-run` does a real run by hand while watching the log.
3. End to end: `docker exec hermes hermes cron run <trigger-id>`, check that launchd picks it up, then `hermes cron run <report-id>`.
4. Failure drills, each of which must leave the old container running and produce an alert:
   - dirty working tree
   - simulated merge conflict (in a scratch branch)
   - broken `fork/Dockerfile`
   - unhealthy container (point the health check at a wrong port)
5. Leave it for a few nights and check `hermes cron list`, `status.json`, and the log.

## Risks and open questions

- **Upstream `main` is trusted nightly.** Whatever lands upstream is built and run automatically. The
  build gate and health check catch breakage, not malice. Alternative: track the latest upstream release
  tag instead of `main` for fewer, vetted updates. This is the user's call; the default is `main`, as requested.
- **Timezone** is assumed to be America/New_York, based on the host's −04:00 offset. Change `HERMES_TIMEZONE` if that's wrong.
- **A nightly build and restart drops in-flight work.** At 04:00 that's acceptable. Hermes's admission
  recovery handles interrupted cron runs.
- **Upstream changes to the `hermes-base` build or the image's `USER`/entrypoint** could break the
  `fork/Dockerfile` layer. The build and health gates catch this.
