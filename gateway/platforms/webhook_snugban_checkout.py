"""The git checkout a Snugban dispatch works in: cloned by the receiver before the dispatch's run is
admitted, so where the work happens does not depend on the model. A fresh folder gets a clone of the
project's repository on the dispatch's branch (tracking it when the remote has it, else branched from the
base branch); a folder that already holds the checkout (a resend after a restart) is fetched, never
re-cloned. Git runs with the host's own credentials (its global config keeps credential helpers such as
``gh auth setup-git``), never prompts, and runs no hooks."""

import logging
import os
import subprocess
from pathlib import Path
from typing import Optional

from hermes_cli._subprocess_compat import harden_git_argv

logger = logging.getLogger(__name__)

_GIT_TIMEOUT_SECONDS = 600
# Hooks and fsmonitor are off for these plumbing calls; credentials still come from the user's config.
_GIT_CONFIG = ("-c", f"core.hooksPath={os.devnull}", "-c", "core.fsmonitor=false")


class CheckoutError(Exception):
    """Why the checkout could not be made: short, and safe to send to Snugban (no output, no secrets)."""


def git_env(base: dict) -> dict:
    """*base* (the served profile's child environment) set so git fails instead of prompting."""
    env = dict(base)
    env.update(GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="Never")
    env.setdefault("GIT_SSH_COMMAND", "ssh -o BatchMode=yes")
    return env


def _git(args: list, cwd: Path, env: dict, *, what: str, ok_codes: tuple = (0,)) -> int:
    result = subprocess.run(["git", *_GIT_CONFIG, *harden_git_argv(args)], cwd=cwd, capture_output=True,
                            text=True, encoding="utf-8", errors="replace", timeout=_GIT_TIMEOUT_SECONDS,
                            stdin=subprocess.DEVNULL, env=env, check=False)
    if result.returncode not in ok_codes:
        logger.warning("[webhook] Snugban checkout: git %s failed (%s): %s", args[0], result.returncode,
                       result.stderr.strip()[-500:])
        raise CheckoutError(f"{what} failed (git exit {result.returncode})")
    return result.returncode


def prepare_checkout(workspace: Path, remote_url: str, repo: str, branch: str, base_branch: Optional[str],
                     env: dict) -> None:
    """Make *workspace* a checkout of *remote_url* on *branch*, or fetch it when it already is one. *env* is
    git's environment (``git_env``)."""
    if (workspace / ".git").is_dir():
        _git(["fetch", "--quiet", "origin"], workspace, env, what=f"fetching {repo}")
        return
    workspace.mkdir(mode=0o700, parents=True, exist_ok=True)
    if any(workspace.iterdir()):
        raise CheckoutError("the dispatch's folder is not empty and holds no checkout")
    _git(["clone", "--quiet", "--", remote_url, "."], workspace, env, what=f"cloning {repo}")
    exists = _git(["ls-remote", "--exit-code", "--heads", "origin", branch], workspace, env,
                  what=f"looking up {branch}", ok_codes=(0, 2)) == 0
    if exists:
        _git(["checkout", "--quiet", "-b", branch, "--track", f"origin/{branch}"], workspace, env,
             what=f"checking out {branch}")
    else:
        start = [f"origin/{base_branch}"] if base_branch else []
        _git(["checkout", "--quiet", "-b", branch, *start], workspace, env, what=f"creating {branch}")
