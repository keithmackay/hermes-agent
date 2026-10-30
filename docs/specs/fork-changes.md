# Fork changes

Everything this fork (`keithmackay/hermes-agent`) adds to or changes from upstream
(`NousResearch/hermes-agent`), newest first. Add an entry with every change. Upstream merges are not
logged here; `git log upstream/main..main` shows what's ours.

**Conventions**
- Fork-only files live in `fork/`. Don't edit upstream files unless there is no other way, and when you
  must, record the file and reason here so merge conflicts are easy to diagnose. (Until plan Phase 1
  lands, the files listed below still sit at the repo root.)
- Plans go in `docs/plans/`. This log and other specs go in `docs/specs/`.

| Date | Commit | Change | Files | Upstream files touched |
|---|---|---|---|---|
| 2026-10-30 | (this commit) | Plan for nightly upstream sync and rebuild (Hermes cron triggers a macOS launchd worker). Started this change log and the fork conventions. | `docs/plans/2026-10-30-nightly-upstream-sync.md`, `docs/specs/fork-changes.md`, `.claude/CLAUDE.md` | none |
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
