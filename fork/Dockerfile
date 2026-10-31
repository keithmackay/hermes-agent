# Fork layer on top of the upstream image (built from the repo root as the
# `hermes-base` context in docker-compose.mac.yml). Keeping our additions here
# leaves upstream's Dockerfile untouched, so nightly upstream merges don't
# conflict with them.
#
# The upstream image already ends as root (its s6 entrypoint drops to the
# hermes user at runtime), so no USER switching is needed.
FROM hermes-base

# ---------- Claude Code CLI ----------
# Lets the agent delegate coding work to `claude` (print mode or PTY) through
# the bundled claude-code skill. Installed root-owned, so the hermes user cannot
# self-update it: bump CLAUDE_CODE_VERSION and rebuild instead (the nightly
# sync does this). Auth comes from CLAUDE_CODE_OAUTH_TOKEN at runtime, which
# the terminal env policy passes through to agent-spawned `claude` processes.
# Two npm guards must be satisfied or the install fails with "native binary
# not installed": the pinned version must be at least 14 days old (.npmrc
# min-release-age, which applies to the platform-native optional dependency
# too), and the package's postinstall must be explicitly allowed (npm blocks
# install scripts by default).
ARG CLAUDE_CODE_VERSION=2.1.302
RUN npm install -g --prefix /usr/local --no-audit --fetch-retries=5 \
        --allow-scripts=@anthropic-ai/claude-code \
        "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" && \
    npm cache clean --force && \
    claude --version
ENV DISABLE_AUTOUPDATER=1
