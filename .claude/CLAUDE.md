# Fork conventions (keithmackay/hermes-agent)

This is a fork of NousResearch/hermes-agent. Upstream's contributor guide is `AGENTS.md`.

- Put fork-only additions in `fork/`. Avoid editing upstream files: upstream is merged in nightly, and
  every edit to an upstream file is a future merge conflict.
- Record every change in `docs/specs/fork-changes.md` (date, commit, files, any upstream files touched).
- Write plans in `docs/plans/YYYY-MM-DD-<topic>.md`.
- Docker Desktop setup: `docker-compose.mac.yml` (moving to `fork/`). Secrets live in the git-ignored `.env.mac`.
