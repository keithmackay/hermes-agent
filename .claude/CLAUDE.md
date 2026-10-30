# Fork conventions (keithmackay/hermes-agent)

This is a fork of NousResearch/hermes-agent. Upstream's contributor guide is `AGENTS.md`.

- Put fork-only additions in `fork/`. Avoid editing upstream files: upstream is merged in nightly, and
  every edit to an upstream file is a future merge conflict.
- Record every change in `docs/specs/fork-changes.md` (date, commit, files, any upstream files touched).
- Write plans in `docs/plans/YYYY-MM-DD-<topic>.md`.
- Docker Desktop: `docker compose -f fork/docker-compose.mac.yml build|up -d`. `fork/Dockerfile` layers
  on the unmodified upstream image.
- Never put secrets inside the repo tree: upstream's `COPY . .` would bake them into the image. They
  live in `~/.config/hermes-fork/env.mac`.
