# Fork changes

Everything this fork (`keithmackay/hermes-agent`) adds to or changes from upstream
(`NousResearch/hermes-agent`), newest first. Add an entry with every change. Upstream merges are not
logged here; `git log upstream/main..main` shows what's ours.

**Conventions**
- Fork-only files live in `fork/`. Don't edit upstream files unless there is no other way, and when you
  must, record the file and reason here so merge conflicts are easy to diagnose.
- Secrets never go inside the repo tree (upstream's `COPY . .` would bake them into the image). They
  live in `~/.config/hermes-fork/`.
- Plans go in `docs/plans/`. This log and other specs go in `docs/specs/`.

**Upstream files currently modified: none** (as of 2026-10-30, sync plan Phase 1).

| Date | Commit | Change | Files | Upstream files touched |
|---|---|---|---|---|
| 2026-10-31 | `335cf1e5a5`, docs in (this commit) | Sync plan Phase 4: Hermes cron scripts `fork/hermes-scripts/fork-sync-trigger.sh` and `fork-sync-report.py`, mounted read-only, plus the shared trigger folder mount. Created jobs `fork-sync-trigger` (`1347d7c5fa27`, 04:00) and `fork-sync-report` (`2c1b3594db1d`, 05:00), delivering to `local`. Verified end to end. The nightly sync is live. | `fork/docker-compose.mac.yml`, `fork/hermes-scripts/*`, docs | none |
| 2026-10-31 | `2b82b2d96f`, docs `1a80d45962` | Sync plan Phase 3: launchd agent (`fork/sync/com.keithmackay.hermes-fork-sync.plist` template plus `install-launchd.sh`), installed and verified (a container-written trigger fires it in about 6 s; a full triggered run deployed `2b82b2d96f`). Added a 36 h no-trigger alarm to `fork-sync.sh`. | `fork/sync/*`, docs | none |
| 2026-10-30 | `766420e53f` | First real sync run (`fork-sync.sh --force-run`), which also replaced the test-clone image from the Phase 2 incident: merged 2,313 upstream commits (`09d03316e8`, clean), bumped Claude Code 2.1.296 → 2.1.302 (`6a10bf922a`), deployed and passed the health check. Images now report `v0.22.5+<distance>.g<sha>`. Also fixed: preflight compared `git remote get-url` output, which a global `url.*.insteadOf` https→ssh rule rewrites (`30c69d29f5`). | `fork/sync/fork-sync.sh`, docs | none |
| 2026-10-30 | `2f70daf595` | Sync plan Phase 2: `fork/sync/fork-sync.sh`, the host-side nightly worker (merge upstream, bump Claude Code to the newest release at least 14 days old, version stamp, build, push, deploy, health check with rollback). Ownership guard so only the checkout that created the container can redeploy it. Automated Claude Code bump commits (`chore(fork): bump Claude Code to X`) are recorded in git and `status.json`, not in this log. | `fork/sync/fork-sync.sh`, docs | none |
| 2026-10-30 | `4d11d4abc0` | Sync plan Phase 1: moved the fork's files into `fork/`. Claude Code moved to `fork/Dockerfile`, a one-layer image on top of the unmodified upstream image (`hermes-base`, built with `scale: 0`). Secrets moved out of the repo to `~/.config/hermes-fork/env.mac`. Added `HERMES_TIMEZONE=America/New_York`. Compose project name pinned to `hermes-agent`. Run with `docker compose -f fork/docker-compose.mac.yml ...`. | `fork/Dockerfile`, `fork/docker-compose.mac.yml`, `fork/env.mac.example` (moved from the root `.env.mac.example`), docs | Reverted both to upstream: `Dockerfile` (Claude Code block removed) and `.gitignore` (`.env.mac` line removed). |
| 2026-10-30 | `f26efbf6ee` | Plan for nightly upstream sync and rebuild (Hermes cron triggers a macOS launchd worker). Started this change log and the fork conventions. | `docs/plans/2026-10-30-nightly-upstream-sync.md`, `docs/specs/fork-changes.md`, `.claude/CLAUDE.md` | none |
| 2026-10-25 | `05aed45ad2` | Baked the Claude Code CLI (2.1.296, auto-update off) into the image so the bundled `claude-code` skill can run `claude -p` and interactive sessions. The Mac compose now builds from this checkout as `hermes-agent:mac-local`. `CLAUDE_CODE_OAUTH_TOKEN` (from `claude setup-token`) goes in `.env.mac`. | `Dockerfile`, `docker-compose.mac.yml`, `.env.mac.example` | `Dockerfile`: Claude Code block after the photon sidecar install. Phase 1 moves it into `fork/Dockerfile`. |
| 2026-10-24 | `297866f933` | Docker Desktop for Mac setup: one container (gateway and dashboard), named volume `hermes-data` (avoids SQLite WAL corruption on virtiofs), ports bound to 127.0.0.1, dashboard basic auth and API key from the git-ignored `.env.mac`. | `docker-compose.mac.yml`, `.env.mac.example` | `.gitignore`: `+.env.mac`. Phase 1 moves it to `fork/.gitignore`. |

## Notes

- **Claude Code version pin.** It must be at least 14 days old because of the repo's `.npmrc`
  `min-release-age`, which also covers the `linux-arm64` platform package. The install also needs
  `--allow-scripts=@anthropic-ai/claude-code`. Missing either one fails the build with "claude native
  binary not installed".
- **Claude Code auth.** Use `CLAUDE_CODE_OAUTH_TOKEN`, which Hermes passes through to agent-spawned
  processes. `ANTHROPIC_API_KEY` is deliberately blocked from them because Hermes treats it as its own
  provider credential.
